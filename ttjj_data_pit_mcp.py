"""ttjj-data-pit: 天天基金数据 API 的「时点版」薄代理.

与 MCP/ttjj-data-mcp/server.py 的区别:
  1. 剔除了无法锚定到日期的工具 (基金筛选/业绩/经理画像/费率/各类基于最新数据的筛选,
     以及 fund_top_holdings / fund_invest_position / fund_turnover_rate /
     fund_industry_exposure 这 4 个只有"报告期"无披露日期的接口本期不纳入).
  2. 每个工具都有必填首参 as_of_date (YYYY-MM-DD): 只返回该日期当天及之前的数据,
     哪怕上游数据库里有更新的也不返回.

注意/已知局限:
  - market_realtime_quote 在 as_of_date 早于今天时, 行情时间戳必然 > as_of_date,
    会被过滤掉, 实际相当于该工具只对"今天"有效.
  - fund_basic_info 的 "基金经理"/"最新定期报告时间" 是当前值, 已置 null;
    本服务不提供 as_of_date 当时的真实任职经理.
  - 响应过滤靠"字段名像日期 + 值能解析成日期"启发式; 上游若改字段名/日期格式可能漏过滤.

启动:
    python ttjj_data_pit_mcp.py            # streamable-http on 127.0.0.1:18078
    python ttjj_data_pit_mcp.py --port 18078 --host 127.0.0.1
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date, datetime
from typing import Any, Optional

import requests
from mcp.server.fastmcp import FastMCP

BASE_URL = "http://ttjj-data-api.jijinmima.cn"
TIMEOUT = 30

# 字段名匹配这些子串(大小写无关) -> 视为"数据日期"字段
_DATE_FIELD_RE = re.compile(r"日期|报告期|登记日|发放日|时间|date|time", re.IGNORECASE)
# ...但这些精确字段名是"接口元数据时间"/"记录维护时间", 不当作数据日期, 不参与过滤
_DATE_FIELD_BLOCKLIST = {
    "query_time", "update_time", "updated_at", "create_time", "created_at",
    "_as_of_date", "_pit_note", "_pit_truncated",
}

_mcp = FastMCP("ttjj-data-pit")
_session: requests.Session | None = None


def _parse_date(value: Any) -> Optional[date]:
    """Best-effort parse to a date; return None if `value` doesn't look like one."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    s = value.strip()
    if len(s) < 4 or not s[:4].isdigit():
        return None
    m3 = re.match(r"(\d{4})\D?(\d{1,2})\D?(\d{1,2})", s)
    if m3:
        try:
            return date(int(m3.group(1)), int(m3.group(2)), int(m3.group(3)))
        except ValueError:
            pass  # fall through to year-month and year-only patterns
    m2 = re.match(r"(\d{4})[-/](\d{1,2})$", s)
    if m2:
        try:
            return date(int(m2.group(1)), int(m2.group(2)), 1)
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}", s):
        return date(int(s), 1, 1)
    return None


def _is_date_field(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    if key in _DATE_FIELD_BLOCKLIST:
        return False
    return bool(_DATE_FIELD_RE.search(key))
