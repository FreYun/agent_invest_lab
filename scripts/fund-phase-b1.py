#!/usr/bin/env python3
"""Phase B-1（薄客户端）— 能力圈宣告闸门 + 范式选择。

实际逻辑（读 能力圈宣告.md → 选 paradigm → 写 fund_paradigm_runs）全在 fund-portfolio-mcp 的
select_fund_paradigm / select_all_fund_paradigms 里。本脚本只通过 MCP 调用，然后把结果打印成
JSON 数组（cron 的 run_phase_b1 现在直接调 MCP，不再跑本脚本；本脚本保留供手动运维用）。

用法：
  python3 fund-phase-b1.py --trade-date 2026-05-08 --run-id cron-x
  python3 fund-phase-b1.py --bot bot1 --trade-date 2026-05-08
  python3 fund-phase-b1.py --mcp-url http://localhost:28073/mcp
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import requests

DEFAULT_MCP_URL = os.getenv("FUND_MCP_ADMIN_URL", "http://localhost:28073/mcp")
TIMEOUT = 120


class MCPClient:
    def __init__(self, url: str):
        self.url, self.session, self.session_id = url, requests.Session(), None

    def _headers(self):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    @staticmethod
    def _parse_sse(text):
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return json.loads(text)

    def init(self):
        r = self.session.post(self.url, headers=self._headers(), timeout=TIMEOUT, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "fund-phase-b1", "version": "1.0"}}})
        r.raise_for_status()
        self.session_id = r.headers.get("Mcp-Session-Id")
        self.session.post(self.url, headers=self._headers(), timeout=TIMEOUT,
                          json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def call(self, tool, args):
        r = self.session.post(self.url, headers=self._headers(), timeout=TIMEOUT, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}})
        r.raise_for_status()
        payload = self._parse_sse(r.text)
        if "error" in payload:
            raise RuntimeError(payload["error"])
        for c in payload.get("result", {}).get("content", []):
            if c.get("type") == "text":
                return json.loads(c["text"])
        return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--bot", help="逗号分隔；缺省 = 所有有账户的 bot")
    parser.add_argument("--mcp-url", default=DEFAULT_MCP_URL)
    args = parser.parse_args()

    client = MCPClient(args.mcp_url)
    client.init()
    if args.bot:
        results = []
        for bid in [b.strip() for b in args.bot.split(",") if b.strip()]:
            results.append(client.call("select_fund_paradigm",
                                       {"bot_id": bid, "trade_date": args.trade_date, "run_id": args.run_id}))
    else:
        res = client.call("select_all_fund_paradigms", {"trade_date": args.trade_date, "run_id": args.run_id})
        results = res.get("results", [])
    for r in results:
        sys.stderr.write(f"[{r.get('bot_id')}] paradigm={r.get('paradigm_active')} ({r.get('reason')})\n")
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
