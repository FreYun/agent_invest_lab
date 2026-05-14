#!/usr/bin/env python3
"""
基金 Phase D：每日收益快照（薄客户端）

实际计算 + 写库全部在 fund-portfolio-mcp 的 record_fund_snapshot / record_all_fund_snapshots
里完成（系统层唯一的基金账户业绩计算入口）。本脚本只负责：
  - 确定 trade_date（默认 = fund_nav 最新交易日）
  - 通过 MCP 调用 record_all_fund_snapshots（或按 --bot 逐 bot 调 record_fund_snapshot）

用法：
  python3 scripts/fund-phase-d.py                  # 所有有账户的 bot，用 fund_nav 最新日
  python3 scripts/fund-phase-d.py --bot bot1,bot2  # 只跑指定 bot
  python3 scripts/fund-phase-d.py --date 2026-05-08
  python3 scripts/fund-phase-d.py --mcp-url http://localhost:28073/mcp
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import uuid
from pathlib import Path

import requests

DB_PATH = "/home/rooot/agent_invest_lab/data/fund.db"
LOG_PATH = "/home/rooot/agent_invest_lab/logs/fund-phase-d.log"
DEFAULT_MCP_URL = os.getenv("FUND_MCP_ADMIN_URL", "http://localhost:28073/mcp")
MCP_TIMEOUT = 120

Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("fund-phase-d")
log.setLevel(logging.INFO)
if not log.handlers:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.handlers = [fh, sh]
    log.propagate = False


# ── 轻量 MCP streamable-http 客户端 ──
class MCPClient:
    def __init__(self, url: str):
        self.url = url
        self.session = requests.Session()
        self.session_id: str | None = None

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    @staticmethod
    def _parse_sse(text: str) -> dict:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return json.loads(text)

    def init(self) -> bool:
        try:
            r = self.session.post(self.url, headers=self._headers(), timeout=MCP_TIMEOUT, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "fund-phase-d", "version": "1.0"}},
            })
            r.raise_for_status()
            self.session_id = r.headers.get("Mcp-Session-Id")
            self.session.post(self.url, headers=self._headers(), timeout=MCP_TIMEOUT, json={
                "jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            return True
        except Exception as e:
            log.error(f"MCP init 失败: {e}")
            return False

    def call(self, tool: str, args: dict) -> dict:
        r = self.session.post(self.url, headers=self._headers(), timeout=MCP_TIMEOUT, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": args}})
        r.raise_for_status()
        payload = self._parse_sse(r.text)
        if "error" in payload:
            raise RuntimeError(payload["error"])
        content = payload.get("result", {}).get("content", [])
        for c in content:
            if c.get("type") == "text":
                return json.loads(c["text"])
        return {}


def get_latest_nav_date() -> str | None:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        row = conn.execute("SELECT MAX(nav_date) AS d FROM fund_nav").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", help="bot_id（逗号分隔），缺省 = 所有有账户的 bot")
    parser.add_argument("--date", help="trade_date YYYY-MM-DD，缺省 = fund_nav 最新日")
    parser.add_argument("--mcp-url", default=DEFAULT_MCP_URL)
    args = parser.parse_args()

    trade_date = args.date or get_latest_nav_date()
    if not trade_date:
        log.error("fund_nav 表为空，无法确定交易日，退出")
        sys.exit(1)
    log.info(f"=== Phase D 启动，trade_date={trade_date}（MCP={args.mcp_url}）===")

    client = MCPClient(args.mcp_url)
    if not client.init():
        log.error("MCP 不可达，退出")
        sys.exit(1)

    try:
        if args.bot:
            bot_ids = [b.strip() for b in args.bot.split(",") if b.strip()]
            for bid in bot_ids:
                res = client.call("record_fund_snapshot", {"bot_id": bid, "trade_date": trade_date})
                if res.get("success"):
                    log.info(f"[{bid}] {trade_date} 快照: total=¥{res.get('total_value'):,.0f} "
                             f"daily={res.get('daily_return_pct'):+.2f}% 累计={res.get('cumulative_return_pct'):+.2f}% "
                             f"回撤={res.get('max_drawdown_pct'):.2f}% 持仓 {res.get('positions')} 只")
                else:
                    log.warning(f"[{bid}] 快照失败: {res.get('message')}")
        else:
            res = client.call("record_all_fund_snapshots", {"trade_date": trade_date})
            if res.get("success"):
                for r in res.get("results", []):
                    if r.get("success"):
                        log.info(f"[{r['bot_id']}] {trade_date} 快照: total=¥{r.get('total_value'):,.0f} "
                                 f"daily={r.get('daily_return_pct'):+.2f}% 累计={r.get('cumulative_return_pct'):+.2f}% "
                                 f"回撤={r.get('max_drawdown_pct'):.2f}% 持仓 {r.get('positions')} 只")
                    else:
                        log.warning(f"[{r.get('bot_id')}] 快照失败: {r.get('message')}")
                log.info(f"=== Phase D 完成: {res.get('ok')}/{res.get('total')} ===")
            else:
                log.error(f"record_all_fund_snapshots 失败: {res}")
                sys.exit(1)
    except Exception as e:
        log.exception(f"Phase D 异常: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
