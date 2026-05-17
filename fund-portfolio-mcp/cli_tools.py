#!/usr/bin/env python3
"""One-shot CLI for system callers (e.g., world simulator) that need to invoke
fund-portfolio-mcp tools without going through MCP HTTP. Bypasses MCP entirely
— imports the underlying async functions and runs them locally against the
same DB.

Usage:
  python cli_tools.py init_fund_account     --bot-id bot1 --run-id <runid> --initial-capital 1000000 [--reset]
  python cli_tools.py settle_pending_orders --bot-id bot1 --run-id <runid> --as-of-date 2024-03-15
  python cli_tools.py close_my_day          --bot-id bot1 --run-id <runid> --trade-date 2024-03-14
  python cli_tools.py get_buyable_funds                # 列出 lab 里 fund_nav 覆盖到的全部可交易代码
  python cli_tools.py get_my_history        --bot-id bot1 --run-id <runid> [--limit 30] [--fund-code 510300]
  python cli_tools.py get_my_performance    --bot-id bot1 --run-id <runid> --as-of-date 2024-03-15 [--daily-series-limit 120]

Stdout is the raw JSON the underlying tool returns (so callers can parse it).
Exit code is 0 on tool invocation success (regardless of the returned
{"success": false} payload — the tool itself ran), 1 on Python-level error.
"""
import argparse
import asyncio
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
)
from db import init_db  # noqa: E402

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

    # 可买基金清单（系统侧 day-1 自动调一次播报给 bot；bot 也能自己再调）
    sub.add_parser("get_buyable_funds")

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
        return await portfolio_get_buyable_funds()
    if args.cmd == "get_my_history":
        return await portfolio_get_my_history(args.bot_id, args.limit, args.fund_code, args.run_id)
    if args.cmd == "get_my_performance":
        return await portfolio_get_my_performance(args.bot_id, args.as_of_date, args.daily_series_limit, args.run_id)
    raise SystemExit(f"unknown cmd {args.cmd!r}")


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
