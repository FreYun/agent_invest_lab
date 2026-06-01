#!/usr/bin/env python3
"""
只回填 fund_nav 历史净值，不触碰其他基金投资链路表。

默认行为：
- 基金范围：fund_info / fund_nav 中全部已跟踪基金代码
- 目标日期：补到指定 end_date
- 写入表：fund_nav

数据源：
- research-mcp /api/fund/nav
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import requests

DB_PATH = "/home/rooot/agent_invest_lab/data/fund.db"
LOG_PATH = "/home/rooot/agent_invest_lab/logs/fund-backfill-nav.log"
RESEARCH_MCP_URL = "http://research-mcp.jijinmima.cn/mcp"
MCP_TIMEOUT = 120
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

_SESSIONS: dict[str, str] = {}

Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("fund-backfill-nav")
log.setLevel(logging.INFO)
if not log.handlers:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.handlers = [fh, sh]
    log.propagate = False


def mcp_init(url: str) -> bool:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "fund-backfill-nav", "version": "1.0"},
        },
    }
    try:
        resp = requests.post(url, json=body, headers=MCP_HEADERS, timeout=10)
        resp.raise_for_status()
        sid = resp.headers.get("mcp-session-id", "")
        if sid:
            _SESSIONS[url] = sid
            try:
                requests.post(
                    url,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                    headers={**MCP_HEADERS, "Mcp-Session-Id": sid},
                    timeout=10,
                )
            except Exception:
                pass
        log.info("MCP init ok: %s", url)
        return True
    except Exception as exc:
        log.error("MCP init failed: %s", exc)
        return False


def mcp_call(url: str, tool_name: str, arguments: dict, timeout: int = MCP_TIMEOUT) -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    headers = dict(MCP_HEADERS)
    sid = _SESSIONS.get(url, "")
    if sid:
        headers["Mcp-Session-Id"] = sid
    resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    resp.encoding = "utf-8"
    resp.raise_for_status()
    for line in resp.text.split("\n"):
        if line.startswith("data: "):
            data = json.loads(line[6:])
            content = data.get("result", {}).get("content", [])
            if content:
                text = content[0].get("text", "")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"raw_text": text}
    data = resp.json()
    content = data.get("result", {}).get("content", [])
    if content:
        text = content[0].get("text", "")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw_text": text}
    raise ValueError(f"无法解析 MCP 响应: {resp.text[:300]}")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def normalize_date(s: str) -> str:
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    datetime.strptime(s, "%Y-%m-%d")
    return s


def parse_float(v):
    if v in (None, "", "--"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_tracked_codes(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT fund_code FROM fund_info
        WHERE fund_code IS NOT NULL AND fund_code != ''
        UNION
        SELECT fund_code FROM fund_nav
        WHERE fund_code IS NOT NULL AND fund_code != ''
        ORDER BY fund_code
        """
    ).fetchall()
    return [r["fund_code"] for r in rows]


def fetch_nav_rows(fund_code: str, start_date: str, end_date: str) -> list[dict]:
    res = mcp_call(
        RESEARCH_MCP_URL,
        "get_fund_nav_and_return",
        {"fund_code": fund_code, "start_date": start_date, "end_date": end_date},
    )
    if not res.get("success"):
        raise ValueError(res.get("message", "fund_nav 返回失败"))

    data = res.get("data") or {}
    columns = data.get("columns") or []
    records = data.get("data") or []
    idx = {name: i for i, name in enumerate(columns)}

    out = []
    for rec in records:
        if not isinstance(rec, (list, tuple)):
            continue
        nav_date = rec[idx["日期"]] if "日期" in idx and idx["日期"] < len(rec) else None
        if not nav_date:
            continue
        # fund_nav.nav 统一存放复权单位净值，用于收益快照和持仓估值的统一口径。
        out.append(
            {
                "fund_code": fund_code,
                "nav_date": nav_date,
                "nav": parse_float(
                    rec[idx["复权单位净值"]]
                    if "复权单位净值" in idx and idx["复权单位净值"] < len(rec)
                    else None
                ),
                "acc_nav": None,
                "daily_return_pct": parse_float(
                    rec[idx["日收益率(%)"]]
                    if "日收益率(%)" in idx and idx["日收益率(%)"] < len(rec)
                    else None
                ),
            }
        )
    return out


def upsert_nav_rows(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO fund_nav
            (fund_code, nav_date, nav, acc_nav, daily_return_pct, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r["fund_code"],
                    r["nav_date"],
                    r["nav"],
                    r["acc_nav"],
                    r["daily_return_pct"],
                    now,
                )
                for r in rows
            ],
        )
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD or YYYYMMDD")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD or YYYYMMDD")
    parser.add_argument("--codes", nargs="*", help="可选，指定基金代码；缺省为全部已跟踪基金")
    args = parser.parse_args()

    start_date = normalize_date(args.start_date)
    end_date = normalize_date(args.end_date)

    conn = get_conn()
    codes = args.codes or get_tracked_codes(conn)
    if not codes:
        log.warning("没有需要回填的基金代码")
        return 0

    if not mcp_init(RESEARCH_MCP_URL):
        return 2

    total_rows = 0
    failed: list[str] = []
    for idx, code in enumerate(codes, start=1):
        try:
            rows = fetch_nav_rows(code, start_date, end_date)
            n = upsert_nav_rows(conn, rows)
            total_rows += n
            log.info("[%d/%d] %s 写入 %d 行", idx, len(codes), code, n)
        except Exception as exc:
            failed.append(code)
            log.error("[%d/%d] %s 失败: %s", idx, len(codes), code, exc)

    log.info("完成: codes=%d, rows=%d, failed=%d", len(codes), total_rows, len(failed))
    if failed:
        log.info("失败代码: %s", ",".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
