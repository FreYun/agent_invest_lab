#!/usr/bin/env python3
"""One-shot CLI for system callers (e.g., world simulator) that need to invoke
fund-portfolio-mcp tools without going through MCP HTTP. Bypasses MCP entirely
— imports the underlying async functions and runs them locally against the
same DB.

Usage:
  python cli_tools.py init_fund_account     --bot-id bot1 --run-id <runid> --initial-capital 1000000 [--reset]
  python cli_tools.py settle_pending_orders --bot-id bot1 --run-id <runid> --as-of-date 2024-03-15
  python cli_tools.py close_my_day          --bot-id bot1 --run-id <runid> --trade-date 2024-03-14
  python cli_tools.py get_buyable_funds [--run-id <runid>] [--bot-id bot1]  # 列出可交易代码；传 bot-id 时按 bot 池收窄
  python cli_tools.py get_my_history        --bot-id bot1 --run-id <runid> [--limit 30] [--fund-code 510300]
  python cli_tools.py get_my_performance    --bot-id bot1 --run-id <runid> --as-of-date 2024-03-15 [--daily-series-limit 120]

Stdout is the raw JSON the underlying tool returns (so callers can parse it).
Exit code is 0 on tool invocation success (regardless of the returned
{"success": false} payload — the tool itself ran), 1 on Python-level error.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

# Make sibling server.py importable when this script is invoked from any cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Importing server.py constructs a FastMCP instance and registers @mcp.tool()
# decorators in module scope — harmless since we never start its transport.
from server import (  # noqa: E402
    portfolio_init_my_account,
    portfolio_close_my_day,
    portfolio_get_buyable_funds,
    portfolio_get_my_history,
    portfolio_get_my_performance,
    settle_pending_fund_orders,
    _fund_fee_rates,
)
from db import get_conn, init_db  # noqa: E402

# CLI path doesn't invoke server.main(), so the CREATE TABLE IF NOT EXISTS
# bootstrap in db.init_db() never runs through MCP — any table added to
# SCHEMA_SQL after fund.db was first created silently doesn't exist when
# CLI tools run (e.g. fund_bot_performance, which portfolio_close_my_day
# and portfolio_get_my_performance query → both error out with "no such
# table"). Force init_db() at module load so schema is current whichever
# command we dispatch.
init_db()


async def _amain() -> str:
    parser = argparse.ArgumentParser(prog="cli_tools.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # init_fund_account 是历史子命令名（保留以兼容 world/src/run.ts 的 spawn 调用）；
    # 实际调用的是新的 portfolio_init_my_account(bot_id, initial_capital, force=False, run_id=...)。
    # --reset 直接传 force=True，让 server.py 自己用同一段清表逻辑（doc:
    # "True：清空该 bot 在全部 7 张业务表的所有行，再重建"），避免 cli 这边重写。
    # --run-id 必填：写入工具（portfolio_init_my_account / settle / close_my_day）都强制要 run_id。
    p_init = sub.add_parser("init_fund_account")
    p_init.add_argument("--bot-id", required=True)
    p_init.add_argument("--initial-capital", type=float, required=True)
    p_init.add_argument("--run-id", required=True,
                        help="本轮 run id（world 透传）。server.py 写入检查必填。")
    p_init.add_argument("--reset", action="store_true",
                        help="force=True: 调 portfolio_init_my_account 内置的 reset 路径，"
                             "把该 bot 在 7 张业务表的所有行清掉再重建。world replay 默认开。")

    # T+1 收口：每天 chat 之前调一次，按 order_date < as_of_date 把昨天及更早的 pending 单
    # 用各自锁定的 reference_nav 收口（BUY → cash_in_transit 释放 + 持仓增加；SELL → 持仓减少 + 现金回流）。
    # close_my_day 不做这件事——它只落快照。
    p_settle = sub.add_parser("settle_pending_orders")
    p_settle.add_argument("--bot-id", required=True)
    p_settle.add_argument("--as-of-date", required=True)
    p_settle.add_argument("--run-id", required=True)

    p_close = sub.add_parser("close_my_day")
    p_close.add_argument("--bot-id", required=True)
    p_close.add_argument("--trade-date", required=True)
    p_close.add_argument("--run-id", required=True)

    # 可买基金清单（系统侧 day-1 自动调一次播报给 bot；bot 也能自己再调）。
    # --run-id 可选：传则按 per-run 白名单收窄（curated=True），不传则返全集（lab ad-hoc 用）。
    p_buy = sub.add_parser("get_buyable_funds")
    p_buy.add_argument("--run-id", default="",
                       help="本轮 run id。传则按 per-run 白名单收窄；不传返全集。")
    p_buy.add_argument("--bot-id", default="",
                       help="可选 bot id。传则优先按 by_bot[bot_id] 返回该 bot 专属可买池。")

    # World daily-prompt 注入用：读账户/持仓/订单快照 + 历史业绩，避免 bot 每天
    # 都自己调 portfolio_get_my_history / portfolio_get_my_performance。底层调
    # server.py 的同名 @mcp.tool 实现，输出格式一致。
    p_hist = sub.add_parser("get_my_history")
    p_hist.add_argument("--bot-id", required=True)
    p_hist.add_argument("--run-id", required=True,
                        help="本轮 run id（world 透传）。read 工具 strict 之后必填。")
    p_hist.add_argument("--limit", type=int, default=30)
    p_hist.add_argument("--fund-code", default="")

    p_perf = sub.add_parser("get_my_performance")
    p_perf.add_argument("--bot-id", required=True)
    p_perf.add_argument("--run-id", required=True,
                        help="本轮 run id（world 透传）。")
    p_perf.add_argument("--as-of-date", required=True)
    p_perf.add_argument("--daily-series-limit", type=int, default=120)

    # World daily-prompt 注入用：批量取 buyable 池的费率（申购费 / 赎回费阶梯 /
    # 管理+托管年化）。世界端不再走 simworld fund_rate 一只一只调，直接读本地
    # fund.db——费率属于静态/半静态字段，PIT 漂移可忽略。
    p_fees = sub.add_parser("get_fund_fees")
    p_fees.add_argument("--fund-codes", required=True,
                        help="逗号分隔的 6 位基金代码，例如 '016729,510300'")

    # World daily-prompt 注入用（multi-fund bot 专用）：批量取 buyable 池的"主题+因子+1y业绩"
    # meta，给多基金 bot 在 prompt 里渲染【可买池主题分布 + 候选样本】。单条 SQL JOIN
    # fund_info + fund_style(latest) + fund_performance(period=1y, as_of <= as_of_date)，
    # 比 bot 自己 865 次 get_fund_detail 高效得多。
    # PIT 说明：fund_style 当前只有 2026-03-31 单截面快照，回测日 < 该日时 style 字段会带
    # 未来信息——属于已知的小漏，size/invest_style 是慢变标签，对策略影响很小。
    p_meta = sub.add_parser("get_pool_meta")
    p_meta.add_argument("--fund-codes", required=True,
                        help="逗号分隔的 6 位基金代码")
    p_meta.add_argument("--as-of-date", required=True,
                        help="回测当日 YYYY-MM-DD；1y 业绩快照取 as_of_date <= 该日的最新一行")

    p_creq = sub.add_parser("charter_require")
    p_creq.add_argument("--bot-id", required=True)
    p_creq.add_argument("--run-id", required=True)

    p_cstat = sub.add_parser("charter_status")
    p_cstat.add_argument("--bot-id", required=True)
    p_cstat.add_argument("--run-id", required=True)
    p_cstat.add_argument("--date", required=True)

    args = parser.parse_args()
    if args.cmd == "init_fund_account":
        return await portfolio_init_my_account(
            args.bot_id,
            args.initial_capital,
            bool(args.reset),
            args.run_id,
        )
    if args.cmd == "settle_pending_orders":
        return await settle_pending_fund_orders(args.bot_id, args.as_of_date, args.run_id)
    if args.cmd == "close_my_day":
        return await portfolio_close_my_day(args.bot_id, args.trade_date, args.run_id)
    if args.cmd == "get_buyable_funds":
        return await portfolio_get_buyable_funds(args.run_id, args.bot_id)
    if args.cmd == "get_my_history":
        return await portfolio_get_my_history(args.bot_id, args.limit, args.fund_code, args.run_id)
    if args.cmd == "get_my_performance":
        return await portfolio_get_my_performance(args.bot_id, args.as_of_date, args.daily_series_limit, args.run_id)
    if args.cmd == "get_fund_fees":
        codes = [c.strip() for c in args.fund_codes.split(",") if c.strip()]
        return _get_fund_fees(codes)
    if args.cmd == "get_pool_meta":
        codes = [c.strip() for c in args.fund_codes.split(",") if c.strip()]
        return _get_pool_meta(codes, args.as_of_date)
    if args.cmd == "charter_require":
        with get_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM fund_bot_charters WHERE bot_id=? AND run_id=? "
                "AND status IN ('required','active') LIMIT 1",
                (args.bot_id, args.run_id)).fetchone()
            if row:
                print(json.dumps({"ok": True, "existing": True}))
            else:
                conn.execute(
                    "INSERT INTO fund_bot_charters (bot_id, run_id, status) VALUES (?,?,'required')",
                    (args.bot_id, args.run_id))
                print(json.dumps({"ok": True, "existing": False}))
        return ""
    if args.cmd == "charter_status":
        with get_conn() as conn:
            active = conn.execute(
                "SELECT declared_date, satellite_review_cadence_days FROM fund_bot_charters "
                "WHERE bot_id=? AND run_id=? AND status='active' ORDER BY charter_id DESC LIMIT 1",
                (args.bot_id, args.run_id)).fetchone()
            required = conn.execute(
                "SELECT 1 FROM fund_bot_charters WHERE bot_id=? AND run_id=? "
                "AND status IN ('required','active') LIMIT 1",
                (args.bot_id, args.run_id)).fetchone() is not None
            out = {"required": required, "declared": active is not None,
                   "cadence": None, "declared_date": None, "last_review_date": None,
                   "review_due": False, "review_overdue": False, "satellite_holding_count": 0}
            if active:
                out["cadence"] = int(active["satellite_review_cadence_days"])
                out["declared_date"] = active["declared_date"]
                last = conn.execute(
                    "SELECT MAX(review_date) AS d FROM fund_bot_satellite_reviews "
                    "WHERE bot_id=? AND run_id=?", (args.bot_id, args.run_id)).fetchone()
                out["last_review_date"] = last["d"]
                baseline = last["d"] or active["declared_date"]
                gap = conn.execute(
                    "SELECT COUNT(DISTINCT nav_date) AS n FROM fund_nav "
                    "WHERE nav_date > ? AND nav_date <= ?", (baseline, args.date)).fetchone()["n"]
                out["review_due"] = gap >= out["cadence"]
                out["review_overdue"] = gap > out["cadence"] + 2
                charter_row = conn.execute(
                    "SELECT core_fund_codes FROM fund_bot_charters WHERE bot_id=? AND run_id=? "
                    "AND status='active' ORDER BY charter_id DESC LIMIT 1",
                    (args.bot_id, args.run_id)).fetchone()
                core = set(json.loads(charter_row["core_fund_codes"] or "[]"))
                rows = conn.execute(
                    "SELECT fund_code FROM fund_bot_holdings WHERE bot_id=? AND run_id=? "
                    "AND status='active' AND shares > 1e-6", (args.bot_id, args.run_id)).fetchall()
                out["satellite_holding_count"] = sum(1 for r in rows if r["fund_code"] not in core)
                if out["satellite_holding_count"] == 0:
                    out["review_due"] = False
                    out["review_overdue"] = False
            print(json.dumps(out, ensure_ascii=False))
        return ""
    raise SystemExit(f"unknown cmd {args.cmd!r}")


def _get_fund_fees(fund_codes: list[str]) -> str:
    """读 fund_info 的费率字段并按 _fund_fee_rates 的口径返回。
    purchase_fee_pct / redeem_tiers 已转成 pct（小数 × 100），方便 prompt 直接渲染。
    mgmt_fee + custody_fee 是 NAV 已扣除的年化管理/托管费率，给 bot 看费用结构。"""
    out = []
    with get_conn() as conn:
        for code in fund_codes:
            row = conn.execute(
                "SELECT fund_name, mgmt_fee, custody_fee, sales_service_fee, purchase_status, redeem_status "
                "FROM fund_info WHERE fund_code=?",
                (code,),
            ).fetchone()
            if not row:
                out.append({"fund_code": code, "fund_name": "", "found": False})
                continue
            pf_rate, redeem_tiers = _fund_fee_rates(conn, code)
            out.append({
                "fund_code": code,
                "fund_name": row["fund_name"] or "",
                "found": True,
                "purchase_fee_pct": round(pf_rate * 100.0, 4),
                "redeem_tiers": [
                    {"max_days": t.get("max_days"), "rate_pct": round(float(t.get("rate") or 0.0) * 100.0, 4)}
                    for t in redeem_tiers
                ],
                "mgmt_fee_pct_annual": float(row["mgmt_fee"] or 0.0),
                "custody_fee_pct_annual": float(row["custody_fee"] or 0.0),
                "sales_service_fee_pct_annual": float(row["sales_service_fee"] or 0.0),
                "purchase_status": row["purchase_status"] or "",
                "redeem_status": row["redeem_status"] or "",
            })
    return json.dumps({"success": True, "fees": out}, ensure_ascii=False)


def _get_pool_meta(fund_codes: list[str], as_of_date: str) -> str:
    """主题+因子+1y业绩 meta（multi-fund bot 的可买池注入）。

    单条 SQL：fund_info LEFT JOIN fund_style(latest) LEFT JOIN fund_performance(1y, PIT)。
    缺失的字段返回 None，渲染端按 None 跳过该列即可（不阻塞 bot）。
    """
    if not fund_codes:
        return json.dumps({"success": True, "pool": []}, ensure_ascii=False)

    out = []
    # SQL 用参数化的 IN 列表（避免 ? 占位符上限和注入风险——code 是 6 位数字，但仍用参数化）
    placeholders = ",".join("?" * len(fund_codes))
    with get_conn() as conn:
        # 1y perf 的 PIT 锚：as_of_date <= 回测当日的最新一行。
        perf_anchor = conn.execute(
            "SELECT MAX(as_of_date) AS d FROM fund_performance WHERE as_of_date <= ?",
            (as_of_date,),
        ).fetchone()
        perf_date = perf_anchor["d"] if perf_anchor else None

        # fund_style 当前只有单截面 2026-03-31，取 MAX(as_of_date)（无视回测日，已知 PIT 漏洞）
        style_anchor = conn.execute(
            "SELECT MAX(as_of_date) AS d FROM fund_style"
        ).fetchone()
        style_date = style_anchor["d"] if style_anchor else None

        # 一条 JOIN 拉全部需要的列
        rows = conn.execute(
            f"""
            SELECT
                fi.fund_code,
                fi.fund_name,
                fi.theme,
                fi.scale,
                fs.size_style,
                fs.invest_style,
                fp.return_pct       AS p1y_return_pct,
                fp.rank_pct         AS p1y_rank_pct,
                fp.rank_text        AS p1y_rank_text,
                fp.max_drawdown_pct AS p1y_mdd_pct,
                fp.sharpe_ratio     AS p1y_sharpe
            FROM fund_info fi
            LEFT JOIN fund_style fs ON fs.fund_code = fi.fund_code AND fs.as_of_date = ?
            LEFT JOIN fund_performance fp ON fp.fund_code = fi.fund_code
                AND fp.period = '1y'
                AND fp.as_of_date = ?
            WHERE fi.fund_code IN ({placeholders})
            """,
            (style_date or "", perf_date or "", *fund_codes),
        ).fetchall()

        for r in rows:
            d = dict(r)
            # scale 是数值（亿元）；空字符串 / None 留作 None
            if d.get("scale") in ("", None):
                d["scale"] = None
            out.append(d)

    return json.dumps({
        "success": True,
        "pool": out,
        "style_as_of": style_date,
        "perf_1y_as_of": perf_date,
    }, ensure_ascii=False)


def main() -> None:
    try:
        out = asyncio.run(_amain())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"cli_tools.py error: {e}", file=sys.stderr)
        raise SystemExit(1) from e
    sys.stdout.write(out if isinstance(out, str) else str(out))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
