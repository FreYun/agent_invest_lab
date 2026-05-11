# ttjj-data-pit MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a "point-in-time" MCP server `ttjj-data-pit` that wraps the same upstream天天基金 data API as `MCP/ttjj-data-mcp/server.py`, but (a) drops tools that can't be anchored to a date, and (b) forces every data read to honor an `as_of_date` so nothing later than that date is ever returned.

**Architecture:** Single-file FastMCP server at `/home/rooot/agent_invest_lab/ttjj_data_pit_mcp.py`. Each kept tool takes a required first param `as_of_date: str (YYYY-MM-DD)`. Before calling upstream we clamp `end_date` to `as_of_date`, reject any user-supplied date param later than `as_of_date` (`lookahead` error), and reject a malformed `as_of_date` (`bad_as_of_date`). After upstream responds we recursively walk the JSON and drop any list-record whose date field is > `as_of_date` (and null out standalone date fields > `as_of_date`), then add `_as_of_date` to the top level. `fund_basic_info` / `stock_profile` get bespoke handling instead of the generic filter; `ttjj_research_search` recomputes its "近 N 天" window.

**Tech Stack:** Python 3.12, `mcp` (FastMCP, streamable-http transport), `requests`, `pytest` for tests. No DB, no cache.

**Reference files (read-only, do not modify):**
- `/home/rooot/MCP/ttjj-data-mcp/server.py` — the original thin proxy; copy its `_get_session` / `_post` / `_err` shape and the exact tool signatures / upstream paths.
- `/home/rooot/MCP/ttjj-data-mcp/restart.sh` — template for our restart script.
- `/home/rooot/agent_invest_lab/docs/superpowers/specs/2026-05-11-ttjj-data-pit-mcp-design.md` — the design this plan implements.

**Final tool set (19 kept):** `fund_nav`, `fund_index_return`, `fund_bonus`, `fund_abnormal_movement`, `market_index_quote`, `stock_market`, `stock_capital_flow`, `stock_ownership`, `stock_financial_quality`, `stock_alpha`, `stock_events`, `macro_data`, `bond_yield_curve`, `commodity_market`, `research_view`, `fund_basic_info`, `stock_profile`, `ttjj_research_search`, `market_realtime_quote`.
**Dropped (14, must NOT appear):** `fund_select`, `fund_performance`, `fund_manager_profile`, `fund_style_analysis`, `fund_rate`, `fund_theme_screening`, `fund_stock_holdings_screen`, `fund_index_tracking`, `fund_top_holdings`, `fund_invest_position`, `fund_turnover_rate`, `fund_industry_exposure`, `entity_extract`, `health_check`.

---

## File Structure

| File | Responsibility |
|---|---|
| `/home/rooot/agent_invest_lab/ttjj_data_pit_mcp.py` | The entire MCP server: constants, date helpers, response filter, as_of guards, `_post`, 19 tools, `main()`. |
| `/home/rooot/agent_invest_lab/requirements.txt` | `mcp`, `requests` (runtime); `pytest` optional / dev. |
| `/home/rooot/agent_invest_lab/restart.sh` | Kill anything on :18078, relaunch the server, log to `/tmp/ttjj-data-pit-mcp.log`. |
| `/home/rooot/agent_invest_lab/.gitignore` | `__pycache__/`, `*.pyc`, `.pytest_cache/`. |
| `/home/rooot/agent_invest_lab/tests/test_helpers.py` | Unit tests for `_parse_date`, `_is_date_field`, `_filter_response`, `_check_as_of`, `_clamp_end_date`, `_reject_if_future`. |
| `/home/rooot/agent_invest_lab/tests/test_tools.py` | Tests for the tool functions with `_post` monkeypatched. |
| `/home/rooot/agent_invest_lab/tests/test_smoke.py` | One opt-in test that hits the live upstream API (skipped unless `TTJJ_PIT_LIVE=1`). |

Everything lives in one module file because the original `server.py` is one file and the logic is small and tightly coupled. Tests are split by what they cover.

---

## Task 1: Project scaffold

**Files:**
- Create: `/home/rooot/agent_invest_lab/.gitignore`
- Create: `/home/rooot/agent_invest_lab/requirements.txt`
- Create: `/home/rooot/agent_invest_lab/ttjj_data_pit_mcp.py` (skeleton: docstring, imports, constants only)
- Create: `/home/rooot/agent_invest_lab/tests/test_helpers.py` (empty placeholder so pytest discovers the dir)

- [ ] **Step 1: Init git repo and install deps**

```bash
cd /home/rooot/agent_invest_lab
git init
python3 -m pip install --quiet mcp requests pytest
```
Expected: `Initialized empty Git repository in /home/rooot/agent_invest_lab/.git/` and pip exits 0 (packages may already be present from the other MCP — that's fine).

- [ ] **Step 2: Write `.gitignore`**

```
__pycache__/
*.pyc
.pytest_cache/
*.log
```

- [ ] **Step 3: Write `requirements.txt`**

```
mcp
requests
pytest
```

- [ ] **Step 4: Write the module skeleton `ttjj_data_pit_mcp.py`**

```python
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
_DATE_FIELD_RE = re.compile(r"日期|时间|date|time", re.IGNORECASE)
# ...但这些精确字段名是"接口元数据时间"/"记录维护时间", 不当作数据日期, 不参与过滤
_DATE_FIELD_BLOCKLIST = {
    "query_time", "update_time", "updated_at", "create_time", "created_at",
    "_as_of_date", "_pit_note", "_pit_truncated",
}

_mcp = FastMCP("ttjj-data-pit")
_session: requests.Session | None = None
```

- [ ] **Step 5: Create the tests dir with a placeholder**

`tests/test_helpers.py`:
```python
def test_placeholder():
    assert True
```

- [ ] **Step 6: Verify pytest runs and commit**

Run: `cd /home/rooot/agent_invest_lab && python3 -m pytest -q`
Expected: `1 passed`

```bash
cd /home/rooot/agent_invest_lab
git add .gitignore requirements.txt ttjj_data_pit_mcp.py tests/test_helpers.py
git commit -m "chore: scaffold ttjj-data-pit MCP project"
```

---

## Task 2: `_parse_date` — lenient date parser

**Files:**
- Modify: `/home/rooot/agent_invest_lab/ttjj_data_pit_mcp.py` (add `_parse_date`)
- Modify: `/home/rooot/agent_invest_lab/tests/test_helpers.py` (replace placeholder)

- [ ] **Step 1: Write the failing tests**

Replace the contents of `tests/test_helpers.py` with:
```python
from datetime import date, datetime

import ttjj_data_pit_mcp as m


class TestParseDate:
    def test_iso(self):
        assert m._parse_date("2024-01-01") == date(2024, 1, 1)

    def test_compact(self):
        assert m._parse_date("20240131") == date(2024, 1, 31)

    def test_slash(self):
        assert m._parse_date("2024/02/05") == date(2024, 2, 5)

    def test_datetime_string(self):
        assert m._parse_date("2024-03-04 09:30:00") == date(2024, 3, 4)

    def test_single_digit_parts(self):
        assert m._parse_date("2024-3-5") == date(2024, 3, 5)

    def test_year_month(self):
        assert m._parse_date("2024-01") == date(2024, 1, 1)

    def test_year_only(self):
        assert m._parse_date("2024") == date(2024, 1, 1)

    def test_date_object_passthrough(self):
        assert m._parse_date(date(2024, 5, 6)) == date(2024, 5, 6)

    def test_datetime_object(self):
        assert m._parse_date(datetime(2024, 5, 6, 12, 0)) == date(2024, 5, 6)

    def test_not_a_date(self):
        assert m._parse_date("hello") is None
        assert m._parse_date("12345") is None
        assert m._parse_date("") is None
        assert m._parse_date(None) is None
        assert m._parse_date(123) is None
        assert m._parse_date(12.5) is None

    def test_invalid_calendar_date(self):
        assert m._parse_date("2024-13-40") is None
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `cd /home/rooot/agent_invest_lab && python3 -m pytest tests/test_helpers.py -q`
Expected: FAIL — `AttributeError: module 'ttjj_data_pit_mcp' has no attribute '_parse_date'`

- [ ] **Step 3: Implement `_parse_date`** (append to `ttjj_data_pit_mcp.py`, after the constants)

```python
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
            return None
    m2 = re.match(r"(\d{4})[-/](\d{1,2})$", s)
    if m2:
        try:
            return date(int(m2.group(1)), int(m2.group(2)), 1)
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}", s):
        return date(int(s), 1, 1)
    return None
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `cd /home/rooot/agent_invest_lab && python3 -m pytest tests/test_helpers.py -q`
Expected: PASS (all `TestParseDate` tests).

Note: `"12345"` — `m3` regex matches `(\d{4})="1234"`, then `\D?` matches "", then `(\d{1,2})` would try "5", then `\D?(\d{1,2})` needs another digit and the string is exhausted → no overall match; `m2` needs a `-`/`/` separator → no match; `\d{4}` fullmatch needs exactly 4 digits → "12345" is 5 → no match → returns None. Good.

- [ ] **Step 5: Commit**

```bash
cd /home/rooot/agent_invest_lab
git add ttjj_data_pit_mcp.py tests/test_helpers.py
git commit -m "feat: add lenient _parse_date helper"
```

---

## Task 3: `_is_date_field` — recognize date-bearing field names

**Files:**
- Modify: `/home/rooot/agent_invest_lab/ttjj_data_pit_mcp.py` (add `_is_date_field`)
- Modify: `/home/rooot/agent_invest_lab/tests/test_helpers.py` (add a test class)

- [ ] **Step 1: Write the failing tests** (append a new class to `tests/test_helpers.py`)

```python
class TestIsDateField:
    def test_chinese_date_names(self):
        for k in ["交易日期", "净值日期", "报告日期", "报告期", "分红日期",
                  "权益登记日", "发放日", "变动日期", "停牌日期", "复牌日期",
                  "成立时间", "上市日期", "时间", "更新时间"]:
            assert m._is_date_field(k), k

    def test_english_date_names(self):
        for k in ["date", "trade_date", "endDate", "datetime", "publish_time", "timestamp"]:
            assert m._is_date_field(k), k

    def test_blocklisted(self):
        for k in ["query_time", "update_time", "updated_at", "create_time",
                  "created_at", "_as_of_date", "_pit_note", "_pit_truncated"]:
            assert not m._is_date_field(k), k

    def test_non_date_names(self):
        for k in ["基金代码", "基金名称", "单位净值", "code", "name", "占净值比例"]:
            assert not m._is_date_field(k), k

    def test_权益登记日_matches_via_日(self):
        # NOTE: "日" alone is NOT in the regex; "权益登记日" must match — verify the
        # implementation includes "登记日"/"发放日" style names. If your regex is
        # `日期|时间|date|time`, "权益登记日" does NOT contain any of those substrings,
        # so this name needs explicit handling. See implementation below.
        assert m._is_date_field("权益登记日")
        assert m._is_date_field("发放日")
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `cd /home/rooot/agent_invest_lab && python3 -m pytest tests/test_helpers.py::TestIsDateField -q`
Expected: FAIL — `AttributeError: ... has no attribute '_is_date_field'`

- [ ] **Step 3: Implement `_is_date_field`**

First widen the constant in `ttjj_data_pit_mcp.py` so names like `权益登记日` / `发放日` / `登记日` are caught (they don't contain `日期`):
```python
_DATE_FIELD_RE = re.compile(r"日期|登记日|发放日|时间|date|time", re.IGNORECASE)
```
Then add:
```python
def _is_date_field(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    if key in _DATE_FIELD_BLOCKLIST:
        return False
    return bool(_DATE_FIELD_RE.search(key))
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `cd /home/rooot/agent_invest_lab && python3 -m pytest tests/test_helpers.py -q`
Expected: PASS (all `TestParseDate` + `TestIsDateField`).

- [ ] **Step 5: Commit**

```bash
cd /home/rooot/agent_invest_lab
git add ttjj_data_pit_mcp.py tests/test_helpers.py
git commit -m "feat: add _is_date_field with date-name regex + blocklist"
```

---

## Task 4: `_filter_response` — recursive point-in-time filter

**Files:**
- Modify: `/home/rooot/agent_invest_lab/ttjj_data_pit_mcp.py` (add `_filter_response`)
- Modify: `/home/rooot/agent_invest_lab/tests/test_helpers.py` (add a test class)

Semantics (from spec §5):
- Walk the structure in place; return the same object.
- For a **list**: keep an element only if it is *not* a dict, OR it is a dict in which *no* recognized date field has a parseable value > cutoff. Recurse into kept elements.
- For a **dict** (whether top-level or reached during recursion): recurse into every value; additionally, for any key that `_is_date_field` and whose value parses to a date > cutoff, set that value to `None` and set `dict["_pit_truncated"] = True`.
- A dict that is itself a *list element* gets the drop-check first (above); if kept, it still gets the standalone-field nulling for any *other* date fields > cutoff that didn't cause a drop — but since "any date field > cutoff" already triggers a drop, in practice a kept list-element dict has no date fields > cutoff, so nothing to null. Top-level / nested dicts that are *not* list elements are where nulling happens (e.g. `data.最新净值日期`).

- [ ] **Step 1: Write the failing tests** (append a new class to `tests/test_helpers.py`)

```python
from datetime import date as _d


class TestFilterResponse:
    def test_drops_future_list_records(self):
        obj = {"items": [
            {"交易日期": "2023-12-29", "单位净值": 1.1},
            {"交易日期": "2024-01-02", "单位净值": 1.2},
            {"交易日期": "2024-06-01", "单位净值": 1.3},
        ]}
        out = m._filter_response(obj, _d(2024, 1, 1))
        assert out["items"] == [{"交易日期": "2023-12-29", "单位净值": 1.1}]

    def test_keeps_records_without_date_field(self):
        obj = {"items": [{"code": "x"}, {"code": "y"}]}
        out = m._filter_response(obj, _d(2024, 1, 1))
        assert out["items"] == [{"code": "x"}, {"code": "y"}]

    def test_keeps_non_dict_list_elements(self):
        obj = {"vals": [1, 2, "2099-01-01"]}
        out = m._filter_response(obj, _d(2024, 1, 1))
        assert out["vals"] == [1, 2, "2099-01-01"]  # bare strings in lists are not filtered

    def test_any_date_field_over_cutoff_drops_the_record(self):
        obj = {"items": [
            {"分红日期": "2023-06-01", "权益登记日": "2023-06-02", "发放日": "2023-06-10"},
            {"分红日期": "2023-12-30", "权益登记日": "2023-12-31", "发放日": "2024-01-05"},
        ]}
        out = m._filter_response(obj, _d(2024, 1, 1))
        assert out["items"] == [
            {"分红日期": "2023-06-01", "权益登记日": "2023-06-02", "发放日": "2023-06-10"}
        ]

    def test_nulls_standalone_future_date_field(self):
        obj = {"data": {"最新净值日期": "2026-05-01", "items": [{"交易日期": "2023-01-01", "v": 1}]}}
        out = m._filter_response(obj, _d(2024, 1, 1))
        assert out["data"]["最新净值日期"] is None
        assert out["data"]["_pit_truncated"] is True
        assert out["data"]["items"] == [{"交易日期": "2023-01-01", "v": 1}]

    def test_keeps_standalone_past_date_field(self):
        obj = {"data": {"最新净值日期": "2023-12-29"}}
        out = m._filter_response(obj, _d(2024, 1, 1))
        assert out["data"]["最新净值日期"] == "2023-12-29"
        assert "_pit_truncated" not in out["data"]

    def test_ignores_blocklisted_time_fields(self):
        obj = {"metadata": {"query_time": "2026-05-11 19:00:00", "service": "x"},
               "items": [{"交易日期": "2023-01-01"}]}
        out = m._filter_response(obj, _d(2024, 1, 1))
        assert out["metadata"]["query_time"] == "2026-05-11 19:00:00"
        assert "_pit_truncated" not in out["metadata"]

    def test_nested_lists(self):
        obj = {"items": [
            {"基金代码": "A", "记录": [{"交易日期": "2023-05-01", "v": 1},
                                      {"交易日期": "2024-05-01", "v": 2}]},
        ]}
        out = m._filter_response(obj, _d(2024, 1, 1))
        assert out["items"][0]["记录"] == [{"交易日期": "2023-05-01", "v": 1}]

    def test_unparseable_date_value_is_left_alone(self):
        obj = {"items": [{"交易日期": "未知", "v": 1}]}
        out = m._filter_response(obj, _d(2024, 1, 1))
        assert out["items"] == [{"交易日期": "未知", "v": 1}]
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `cd /home/rooot/agent_invest_lab && python3 -m pytest tests/test_helpers.py::TestFilterResponse -q`
Expected: FAIL — `AttributeError: ... has no attribute '_filter_response'`

- [ ] **Step 3: Implement `_filter_response`**

```python
def _record_has_future_date(d: dict, cutoff: date) -> bool:
    for k, v in d.items():
        if _is_date_field(k):
            parsed = _parse_date(v)
            if parsed is not None and parsed > cutoff:
                return True
    return False


def _filter_response(obj: Any, cutoff: date) -> Any:
    """Recursively drop future-dated list records and null future standalone date fields.

    Mutates `obj` in place and returns it.
    """
    if isinstance(obj, list):
        kept = []
        for el in obj:
            if isinstance(el, dict) and _record_has_future_date(el, cutoff):
                continue
            kept.append(_filter_response(el, cutoff))
        obj[:] = kept
        return obj
    if isinstance(obj, dict):
        truncated = False
        for k in list(obj.keys()):
            v = obj[k]
            if _is_date_field(k):
                parsed = _parse_date(v)
                if parsed is not None and parsed > cutoff:
                    obj[k] = None
                    truncated = True
                    continue
            obj[k] = _filter_response(v, cutoff)
        if truncated:
            obj["_pit_truncated"] = True
        return obj
    return obj
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `cd /home/rooot/agent_invest_lab && python3 -m pytest tests/test_helpers.py -q`
Expected: PASS (all helper tests so far).

- [ ] **Step 5: Commit**

```bash
cd /home/rooot/agent_invest_lab
git add ttjj_data_pit_mcp.py tests/test_helpers.py
git commit -m "feat: add _filter_response recursive point-in-time filter"
```

---

## Task 5: `as_of_date` guards — `_check_as_of`, `_clamp_end_date`, `_reject_if_future`

**Files:**
- Modify: `/home/rooot/agent_invest_lab/ttjj_data_pit_mcp.py` (add the three helpers)
- Modify: `/home/rooot/agent_invest_lab/tests/test_helpers.py` (add a test class)

- [ ] **Step 1: Write the failing tests** (append a new class to `tests/test_helpers.py`)

```python
class TestAsOfGuards:
    def test_check_as_of_ok(self):
        cutoff, err = m._check_as_of("2024-01-01")
        assert cutoff == _d(2024, 1, 1)
        assert err is None

    def test_check_as_of_bad(self):
        cutoff, err = m._check_as_of("2024-13-40")
        assert cutoff is None
        assert err == {"error": "bad_as_of_date", "message": "2024-13-40"}

    def test_check_as_of_empty(self):
        cutoff, err = m._check_as_of("")
        assert cutoff is None and err["error"] == "bad_as_of_date"

    def test_clamp_end_date_none_returns_cutoff(self):
        assert m._clamp_end_date(None, _d(2024, 1, 1)) == "2024-01-01"

    def test_clamp_end_date_future_clamped(self):
        assert m._clamp_end_date("2025-06-01", _d(2024, 1, 1)) == "2024-01-01"

    def test_clamp_end_date_past_kept(self):
        assert m._clamp_end_date("2023-06-01", _d(2024, 1, 1)) == "2023-06-01"

    def test_clamp_end_date_unparseable_falls_back_to_cutoff(self):
        assert m._clamp_end_date("不是日期", _d(2024, 1, 1)) == "2024-01-01"

    def test_reject_if_future_none(self):
        assert m._reject_if_future("start_date", None, _d(2024, 1, 1)) is None

    def test_reject_if_future_past_ok(self):
        assert m._reject_if_future("start_date", "2023-01-01", _d(2024, 1, 1)) is None

    def test_reject_if_future_future(self):
        err = m._reject_if_future("report_date", "2025-12-31", _d(2024, 1, 1))
        assert err == {"error": "lookahead",
                       "message": "report_date=2025-12-31 晚于 as_of_date=2024-01-01"}

    def test_reject_if_future_unparseable_passes(self):
        assert m._reject_if_future("trade_date", "garbage", _d(2024, 1, 1)) is None
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `cd /home/rooot/agent_invest_lab && python3 -m pytest tests/test_helpers.py::TestAsOfGuards -q`
Expected: FAIL — `AttributeError: ... has no attribute '_check_as_of'`

- [ ] **Step 3: Implement the three helpers**

```python
def _check_as_of(as_of_date: Any) -> tuple[Optional[date], Optional[dict]]:
    parsed = _parse_date(as_of_date)
    if parsed is None:
        return None, {"error": "bad_as_of_date", "message": str(as_of_date)}
    return parsed, None


def _clamp_end_date(user_end: Optional[str], cutoff: date) -> str:
    if user_end:
        parsed = _parse_date(user_end)
        if parsed is not None and parsed <= cutoff:
            return parsed.isoformat()
    return cutoff.isoformat()


def _reject_if_future(name: str, value: Optional[str], cutoff: date) -> Optional[dict]:
    if not value:
        return None
    parsed = _parse_date(value)
    if parsed is not None and parsed > cutoff:
        return {"error": "lookahead",
                "message": f"{name}={value} 晚于 as_of_date={cutoff.isoformat()}"}
    return None
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `cd /home/rooot/agent_invest_lab && python3 -m pytest tests/test_helpers.py -q`
Expected: PASS (all helper tests).

- [ ] **Step 5: Commit**

```bash
cd /home/rooot/agent_invest_lab
git add ttjj_data_pit_mcp.py tests/test_helpers.py
git commit -m "feat: add as_of_date guard helpers"
```

---

## Task 6: HTTP plumbing — `_get_session`, `_post`, `_pit_wrap`, `_err`

**Files:**
- Modify: `/home/rooot/agent_invest_lab/ttjj_data_pit_mcp.py` (add the four functions)
- Create: `/home/rooot/agent_invest_lab/tests/test_tools.py`

`_post` mirrors `server.py`'s exactly (no as_of awareness). `_pit_wrap` is new: applies `_filter_response` (unless `generic_filter=False`) and stamps `_as_of_date`.

- [ ] **Step 1: Write the failing tests** (`tests/test_tools.py`, new file)

```python
from datetime import date

import pytest

import ttjj_data_pit_mcp as m


class TestPitWrap:
    def test_filters_and_stamps(self):
        result = {"items": [{"交易日期": "2023-12-31", "v": 1}, {"交易日期": "2099-01-01", "v": 2}]}
        out = m._pit_wrap(result, date(2024, 1, 1), "2024-01-01")
        assert out["items"] == [{"交易日期": "2023-12-31", "v": 1}]
        assert out["_as_of_date"] == "2024-01-01"

    def test_skips_generic_filter_when_disabled(self):
        result = {"items": [{"交易日期": "2099-01-01", "v": 2}]}
        out = m._pit_wrap(result, date(2024, 1, 1), "2024-01-01", generic_filter=False)
        assert out["items"] == [{"交易日期": "2099-01-01", "v": 2}]
        assert out["_as_of_date"] == "2024-01-01"

    def test_stamps_error_dicts_too(self):
        out = m._pit_wrap({"error": "api_error", "message": "x"}, date(2024, 1, 1), "2024-01-01")
        assert out["error"] == "api_error"
        assert out["_as_of_date"] == "2024-01-01"


def test_post_uses_base_url_and_unwraps(monkeypatch):
    captured = {}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"success": True, "items": [1, 2]}

    class FakeSession:
        headers = {}
        def post(self, url, data=None, timeout=None):
            captured["url"] = url
            captured["body"] = data
            return FakeResp()

    monkeypatch.setattr(m, "_session", FakeSession())
    out = m._post("/api/fund/nav", {"fund_codes": ["110011"]})
    assert captured["url"] == "http://ttjj-data-api.jijinmima.cn/api/fund/nav"
    assert out == {"success": True, "items": [1, 2]}


def test_post_api_error(monkeypatch):
    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"success": False, "message": "boom"}

    class FakeSession:
        headers = {}
        def post(self, *a, **k): return FakeResp()

    monkeypatch.setattr(m, "_session", FakeSession())
    assert m._post("/x", {}) == {"error": "api_error", "message": "boom"}
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `cd /home/rooot/agent_invest_lab && python3 -m pytest tests/test_tools.py -q`
Expected: FAIL — `AttributeError: ... has no attribute '_pit_wrap'` (and `_post`).

- [ ] **Step 3: Implement the four functions** (append to `ttjj_data_pit_mcp.py`)

```python
def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"Content-Type": "application/json"})
    return _session


def _post(path: str, data: dict) -> dict:
    r = _get_session().post(f"{BASE_URL}{path}", data=json.dumps(data), timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        return {"error": "api_error", "message": j.get("message", "unknown")}
    return j


def _pit_wrap(result: dict, cutoff: date, as_of_label: str, *, generic_filter: bool = True) -> dict:
    if isinstance(result, dict) and generic_filter and "error" not in result:
        _filter_response(result, cutoff)
    if isinstance(result, dict):
        result["_as_of_date"] = as_of_label
    return result


def _err(e: Exception) -> dict[str, Any]:
    return {"error": "request_error", "message": str(e)}
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `cd /home/rooot/agent_invest_lab && python3 -m pytest -q`
Expected: PASS (all helper + tools-plumbing tests).

- [ ] **Step 5: Commit**

```bash
cd /home/rooot/agent_invest_lab
git add ttjj_data_pit_mcp.py tests/test_tools.py
git commit -m "feat: add HTTP plumbing (_post) and _pit_wrap"
```

---

## Task 7: The 15 tools that already had a date parameter

**Files:**
- Modify: `/home/rooot/agent_invest_lab/ttjj_data_pit_mcp.py` (add 15 `@_mcp.tool()` functions)
- Modify: `/home/rooot/agent_invest_lab/tests/test_tools.py` (add a test class)

Each tool: parse `as_of_date` (return `bad_as_of_date` on failure); reject any user-supplied `start_date` later than the cutoff and any user-supplied single-point date later than the cutoff (`lookahead`); clamp `end_date` to the cutoff; call `_post`; return `_pit_wrap(...)`. The upstream paths and the non-date params are copied verbatim from `MCP/ttjj-data-mcp/server.py`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_tools.py`)

```python
class TestDatedTools:
    def _fake_post(self, monkeypatch):
        calls = []
        def fake(path, data):
            calls.append((path, dict(data)))
            return {"success": True, "items": [
                {"交易日期": "2023-12-31", "v": 1},
                {"交易日期": "2099-01-01", "v": 2},
            ]}
        monkeypatch.setattr(m, "_post", fake)
        return calls

    def test_fund_nav_clamps_end_and_filters(self, monkeypatch):
        calls = self._fake_post(monkeypatch)
        out = m.fund_nav("2024-01-01", ["110011"], start_date="2023-01-01", end_date="2025-09-09")
        assert calls[0][0] == "/api/fund/nav"
        assert calls[0][1]["end_date"] == "2024-01-01"          # clamped
        assert calls[0][1]["start_date"] == "2023-01-01"        # kept
        assert out["items"] == [{"交易日期": "2023-12-31", "v": 1}]   # future record dropped
        assert out["_as_of_date"] == "2024-01-01"

    def test_fund_nav_defaults_end_to_as_of(self, monkeypatch):
        calls = self._fake_post(monkeypatch)
        m.fund_nav("2024-01-01", ["110011"])
        assert calls[0][1]["end_date"] == "2024-01-01"

    def test_bad_as_of(self, monkeypatch):
        self._fake_post(monkeypatch)
        assert m.fund_nav("nope", ["110011"]) == {"error": "bad_as_of_date", "message": "nope"}

    def test_future_start_date_rejected(self, monkeypatch):
        self._fake_post(monkeypatch)
        out = m.fund_nav("2024-01-01", ["110011"], start_date="2025-01-01")
        assert out["error"] == "lookahead"

    def test_stock_ownership_future_report_date_rejected(self, monkeypatch):
        self._fake_post(monkeypatch)
        out = m.stock_ownership("2024-01-01", ["600519"], report_date="2025-12-31")
        assert out["error"] == "lookahead"

    def test_stock_ownership_defaults_report_date(self, monkeypatch):
        calls = self._fake_post(monkeypatch)
        m.stock_ownership("2024-01-01", ["600519"])
        assert calls[0][1]["report_date"] == "2024-01-01"

    def test_bond_yield_curve_defaults_date(self, monkeypatch):
        calls = self._fake_post(monkeypatch)
        m.bond_yield_curve("2024-01-01")
        assert calls[0][1] == {"date": "2024-01-01"}

    def test_macro_data_passthrough(self, monkeypatch):
        calls = self._fake_post(monkeypatch)
        m.macro_data("2024-01-01", "cn", categories=["cpi"], start_date="2020-01-01")
        assert calls[0][1]["region"] == "cn"
        assert calls[0][1]["categories"] == ["cpi"]
        assert calls[0][1]["end_date"] == "2024-01-01"

    def test_all_15_registered(self):
        names = {
            "fund_nav", "fund_index_return", "fund_bonus", "fund_abnormal_movement",
            "market_index_quote", "stock_market", "stock_capital_flow", "stock_ownership",
            "stock_financial_quality", "stock_alpha", "stock_events", "macro_data",
            "bond_yield_curve", "commodity_market", "research_view",
        }
        for n in names:
            assert callable(getattr(m, n)), n
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `cd /home/rooot/agent_invest_lab && python3 -m pytest tests/test_tools.py::TestDatedTools -q`
Expected: FAIL — `AttributeError: ... has no attribute 'fund_nav'`

- [ ] **Step 3: Implement all 15 tools** (append to `ttjj_data_pit_mcp.py`)

```python
# ===========================================================================
#  基金 — 有日期参数
# ===========================================================================

@_mcp.tool()
def fund_nav(
    as_of_date: str,
    fund_codes: list[str],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """基金净值历史（时点版：只返回 as_of_date 当天及之前；end_date 会被裁剪到 as_of_date）。"""
    cutoff, err = _check_as_of(as_of_date)
    if err:
        return err
    if (e := _reject_if_future("start_date", start_date, cutoff)):
        return e
    try:
        d: dict = {"fund_codes": fund_codes}
        if start_date:
            d["start_date"] = start_date
        d["end_date"] = _clamp_end_date(end_date, cutoff)
        return _pit_wrap(_post("/api/fund/nav", d), cutoff, as_of_date)
    except Exception as e:
        return _err(e)


@_mcp.tool()
def fund_index_return(
    as_of_date: str,
    fund_codes: list[str],
    trade_date: Optional[str] = None,
    period_codes: Optional[list[str]] = None,
) -> dict[str, Any]:
    """基金指数超额收益（时点版）。period_codes: 00近一周/01近一月/02近三月/03近一年/05近两年/06近三年。"""
    cutoff, err = _check_as_of(as_of_date)
    if err:
        return err
    if (e := _reject_if_future("trade_date", trade_date, cutoff)):
        return e
    try:
        d: dict = {"fund_codes": fund_codes, "trade_date": trade_date or cutoff.isoformat()}
        if period_codes:
            d["period_codes"] = period_codes
        return _pit_wrap(_post("/api/fund/index-return", d), cutoff, as_of_date)
    except Exception as e:
        return _err(e)


@_mcp.tool()
def fund_bonus(as_of_date: str, fund_code: str, date: Optional[str] = None) -> dict[str, Any]:
    """基金分红记录（时点版）。"""
    cutoff, err = _check_as_of(as_of_date)
    if err:
        return err
    if (e := _reject_if_future("date", date, cutoff)):
        return e
    try:
        return _pit_wrap(_post("/api/fund/bonus", {"fund_code": fund_code, "date": date or cutoff.isoformat()}),
                         cutoff, as_of_date)
    except Exception as e:
        return _err(e)


@_mcp.tool()
def fund_abnormal_movement(
    as_of_date: str,
    fund_code: str,
    start_date: Optional[str] = None,
    direction: Optional[str] = None,
) -> dict[str, Any]:
    """基金异动检测（时点版）。direction: '大涨' 或 '跳水'。注意 as_of_date 之后的异动不会返回。"""
    cutoff, err = _check_as_of(as_of_date)
    if err:
        return err
    if (e := _reject_if_future("start_date", start_date, cutoff)):
        return e
    try:
        d: dict = {"fund_code": fund_code}
        if start_date:
            d["start_date"] = start_date
        if direction:
            d["direction"] = direction
        return _pit_wrap(_post("/api/fund/abnormal-movement", d), cutoff, as_of_date)
    except Exception as e:
        return _err(e)


# ===========================================================================
#  市场 / 商品 / 债券
# ===========================================================================

@_mcp.tool()
def market_index_quote(
    as_of_date: str,
    market: str,
    symbols: list[str],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """指数行情（时点版）。market 必须小写: cn/hk/us。港股可传中文名如 '恒生指数'。"""
    cutoff, err = _check_as_of(as_of_date)
    if err:
        return err
    if (e := _reject_if_future("start_date", start_date, cutoff)):
        return e
    try:
        d: dict = {"market": market, "symbols": symbols}
        if start_date:
            d["start_date"] = start_date
        d["end_date"] = _clamp_end_date(end_date, cutoff)
        return _pit_wrap(_post("/api/market/index-quote", d), cutoff, as_of_date)
    except Exception as e:
        return _err(e)


@_mcp.tool()
def commodity_market(
    as_of_date: str,
    symbols: Optional[list[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """商品行情（时点版）。symbols 可传中文如 '黄金', '原油'。"""
    cutoff, err = _check_as_of(as_of_date)
    if err:
        return err
    if (e := _reject_if_future("start_date", start_date, cutoff)):
        return e
    try:
        d: dict = {}
        if symbols:
            d["symbols"] = symbols
        if start_date:
            d["start_date"] = start_date
        d["end_date"] = _clamp_end_date(end_date, cutoff)
        return _pit_wrap(_post("/api/commodity/market", d), cutoff, as_of_date)
    except Exception as e:
        return _err(e)


@_mcp.tool()
def bond_yield_curve(as_of_date: str, date: Optional[str] = None) -> dict[str, Any]:
    """国债收益率曲线（时点版）。不传 date 则取 as_of_date 当天的曲线。"""
    cutoff, err = _check_as_of(as_of_date)
    if err:
        return err
    if (e := _reject_if_future("date", date, cutoff)):
        return e
    try:
        return _pit_wrap(_post("/api/bond/yield-curve", {"date": date or cutoff.isoformat()}),
                         cutoff, as_of_date)
    except Exception as e:
        return _err(e)


# ===========================================================================
#  股票
# ===========================================================================

@_mcp.tool()
def stock_market(
    as_of_date: str,
    stock_codes: list[str],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    trade_date: Optional[str] = None,
) -> dict[str, Any]:
    """股票行情（时点版）：行情记录[] + 市值记录[] + 估值记录[] + 股息率记录[]。"""
    cutoff, err = _check_as_of(as_of_date)
    if err:
        return err
    if (e := _reject_if_future("start_date", start_date, cutoff)):
        return e
    if (e := _reject_if_future("trade_date", trade_date, cutoff)):
        return e
    try:
        d: dict = {"stock_codes": stock_codes}
        if start_date:
            d["start_date"] = start_date
        d["end_date"] = _clamp_end_date(end_date, cutoff)
        if trade_date:
            d["trade_date"] = trade_date
        return _pit_wrap(_post("/api/stock/market", d), cutoff, as_of_date)
    except Exception as e:
        return _err(e)


@_mcp.tool()
def stock_capital_flow(
    as_of_date: str,
    stock_codes: list[str],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """股票资金流向（时点版）：资金流记录[] + 北向持股记录[] + 超大单资金记录[]。"""
    cutoff, err = _check_as_of(as_of_date)
    if err:
        return err
    if (e := _reject_if_future("start_date", start_date, cutoff)):
        return e
    try:
        d: dict = {"stock_codes": stock_codes}
        if start_date:
            d["start_date"] = start_date
        d["end_date"] = _clamp_end_date(end_date, cutoff)
        return _pit_wrap(_post("/api/stock/capital-flow", d), cutoff, as_of_date)
    except Exception as e:
        return _err(e)


@_mcp.tool()
def stock_ownership(
    as_of_date: str,
    stock_codes: list[str],
    report_date: Optional[str] = None,
) -> dict[str, Any]:
    """股权结构（时点版）：股东记录[] + 股本结构记录[] + 股权分配记录[]。"""
    cutoff, err = _check_as_of(as_of_date)
    if err:
        return err
    if (e := _reject_if_future("report_date", report_date, cutoff)):
        return e
    try:
        return _pit_wrap(
            _post("/api/stock/ownership", {"stock_codes": stock_codes, "report_date": report_date or cutoff.isoformat()}),
            cutoff, as_of_date)
    except Exception as e:
        return _err(e)


@_mcp.tool()
def stock_financial_quality(
    as_of_date: str,
    stock_codes: list[str],
    trade_date: Optional[str] = None,
    d_type: str = "TTM",
) -> dict[str, Any]:
    """财务质量（时点版）：盈利能力记录[] + 收益质量记录[] 等。d_type: TTM/ANNUAL/QUARTERLY。"""
    cutoff, err = _check_as_of(as_of_date)
    if err:
        return err
    if (e := _reject_if_future("trade_date", trade_date, cutoff)):
        return e
    try:
        return _pit_wrap(
            _post("/api/stock/financial-quality",
                  {"stock_codes": stock_codes, "d_type": d_type, "trade_date": trade_date or cutoff.isoformat()}),
            cutoff, as_of_date)
    except Exception as e:
        return _err(e)


@_mcp.tool()
def stock_alpha(
    as_of_date: str,
    stock_codes: list[str],
    trade_date: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """Alpha 因子（时点版）：一致预期记录[] + Barra暴露记录[] 等。"""
    cutoff, err = _check_as_of(as_of_date)
    if err:
        return err
    if (e := _reject_if_future("trade_date", trade_date, cutoff)):
        return e
    if (e := _reject_if_future("start_date", start_date, cutoff)):
        return e
    try:
        d: dict = {"stock_codes": stock_codes}
        if trade_date:
            d["trade_date"] = trade_date
        if start_date:
            d["start_date"] = start_date
        d["end_date"] = _clamp_end_date(end_date, cutoff)
        return _pit_wrap(_post("/api/stock/alpha", d), cutoff, as_of_date)
    except Exception as e:
        return _err(e)


@_mcp.tool()
def stock_events(
    as_of_date: str,
    stock_codes: list[str],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """股票事件（时点版）：停复牌记录[] + SUE记录[]。"""
    cutoff, err = _check_as_of(as_of_date)
    if err:
        return err
    if (e := _reject_if_future("start_date", start_date, cutoff)):
        return e
    try:
        d: dict = {"stock_codes": stock_codes}
        if start_date:
            d["start_date"] = start_date
        d["end_date"] = _clamp_end_date(end_date, cutoff)
        return _pit_wrap(_post("/api/stock/events", d), cutoff, as_of_date)
    except Exception as e:
        return _err(e)


# ===========================================================================
#  宏观 / 研究
# ===========================================================================

@_mcp.tool()
def macro_data(
    as_of_date: str,
    region: str,
    categories: Optional[list[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """宏观数据（时点版）。region: cn/us。categories 小写: gdp, cpi, pmi, m2, social_finance, exchange_rate 等。"""
    cutoff, err = _check_as_of(as_of_date)
    if err:
        return err
    if (e := _reject_if_future("start_date", start_date, cutoff)):
        return e
    try:
        d: dict = {"region": region}
        if categories:
            d["categories"] = categories
        if start_date:
            d["start_date"] = start_date
        d["end_date"] = _clamp_end_date(end_date, cutoff)
        return _pit_wrap(_post("/api/macro/data", d), cutoff, as_of_date)
    except Exception as e:
        return _err(e)


@_mcp.tool()
def research_view(
    as_of_date: str,
    view_type: str,
    labels: Optional[list[str]] = None,
    sec_codes: Optional[list[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    direction: Optional[int] = None,
    latest_only: bool = True,
    weeks: int = 1,
    fund_code: Optional[str] = None,
    days: int = 7,
) -> dict[str, Any]:
    """研究观点（时点版）。view_type: sector(行业观点,需labels)/weekly(周报,需labels)/fund_related(需fund_code)。"""
    cutoff, err = _check_as_of(as_of_date)
    if err:
        return err
    if (e := _reject_if_future("start_date", start_date, cutoff)):
        return e
    try:
        d: dict = {"view_type": view_type, "latest_only": latest_only, "weeks": weeks, "days": days}
        if labels:
            d["labels"] = labels
        if sec_codes:
            d["sec_codes"] = sec_codes
        if start_date:
            d["start_date"] = start_date
        d["end_date"] = _clamp_end_date(end_date, cutoff)
        if direction is not None:
            d["direction"] = direction
        if fund_code:
            d["fund_code"] = fund_code
        return _pit_wrap(_post("/api/research/view", d), cutoff, as_of_date)
    except Exception as e:
        return _err(e)
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `cd /home/rooot/agent_invest_lab && python3 -m pytest -q`
Expected: PASS (all helper + plumbing + dated-tools tests).

- [ ] **Step 5: Commit**

```bash
cd /home/rooot/agent_invest_lab
git add ttjj_data_pit_mcp.py tests/test_tools.py
git commit -m "feat: add the 15 dated-parameter point-in-time tools"
```

---

## Task 8: `fund_basic_info` and `stock_profile` (bespoke handling)

**Files:**
- Modify: `/home/rooot/agent_invest_lab/ttjj_data_pit_mcp.py` (add `_mask_keys` helper + 2 tools)
- Modify: `/home/rooot/agent_invest_lab/tests/test_tools.py` (add a test class)

Rationale (spec §3.2②): these have no input date param but their records carry a static date (`成立时间` / `上市日期`). We:
- Run the generic `_filter_response` — which already drops a record if a recognized date field (incl. `成立时间` / `上市日期`) is > cutoff. Wait — that's a problem for `fund_basic_info`, because it *also* has `最新定期报告时间` which is a *current* value (often > cutoff) and would cause the whole record to be dropped. So for these two we call `_pit_wrap(..., generic_filter=False)` and do bespoke filtering instead:
  - `fund_basic_info`: keep a record iff it has no `成立时间`, or `成立时间 <= cutoff`. In kept records, null any key named `基金经理` or `最新定期报告时间` and add `_pit_note`.
  - `stock_profile`: keep a record iff it has no `上市日期`, or `上市日期 <= cutoff`. (No field masking.)
- The records live under `result["items"]` per the upstream shape (see `server.py` comments / `ttjj-api-master`). Handle both `result["items"]` and `result["data"]["items"]` defensively.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_tools.py`)

```python
class TestBespokeTools:
    def test_fund_basic_info_drops_funds_established_after_cutoff_and_masks(self, monkeypatch):
        def fake(path, data):
            assert path == "/api/fund/basic-info"
            return {"success": True, "items": [
                {"基金代码": "110011", "基金公司": "易方达", "基金经理": "张三",
                 "成立时间": "2008-09-19", "最新定期报告时间": "2026-03-31"},
                {"基金代码": "999999", "基金公司": "新基金", "基金经理": "李四",
                 "成立时间": "2025-06-01", "最新定期报告时间": "2026-03-31"},
            ]}
        monkeypatch.setattr(m, "_post", fake)
        out = m.fund_basic_info("2024-01-01", ["110011", "999999"])
        assert len(out["items"]) == 1
        rec = out["items"][0]
        assert rec["基金代码"] == "110011"
        assert rec["基金经理"] is None
        assert rec["最新定期报告时间"] is None
        assert "_pit_note" in rec
        assert out["_as_of_date"] == "2024-01-01"

    def test_stock_profile_drops_stocks_listed_after_cutoff(self, monkeypatch):
        def fake(path, data):
            assert path == "/api/stock/profile"
            return {"success": True, "items": [
                {"股票代码": "600519", "上市日期": "2001-08-27"},
                {"股票代码": "688981", "上市日期": "2025-01-10"},
            ]}
        monkeypatch.setattr(m, "_post", fake)
        out = m.stock_profile("2024-01-01", ["600519", "688981"])
        assert [r["股票代码"] for r in out["items"]] == ["600519"]

    def test_bespoke_bad_as_of(self, monkeypatch):
        monkeypatch.setattr(m, "_post", lambda *a, **k: {"success": True, "items": []})
        assert m.fund_basic_info("xx", ["1"]) == {"error": "bad_as_of_date", "message": "xx"}
        assert m.stock_profile("xx", ["1"]) == {"error": "bad_as_of_date", "message": "xx"}
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `cd /home/rooot/agent_invest_lab && python3 -m pytest tests/test_tools.py::TestBespokeTools -q`
Expected: FAIL — `AttributeError: ... has no attribute 'fund_basic_info'`

- [ ] **Step 3: Implement the helper + 2 tools** (append to `ttjj_data_pit_mcp.py`)

```python
_PIT_MASK_NOTE = "字段已按时点屏蔽: 该值为当前快照, 非 as_of_date 当时的真实值"


def _items_of(result: dict) -> Optional[list]:
    """Return the list of records inside a typical upstream response, or None."""
    if not isinstance(result, dict):
        return None
    if isinstance(result.get("items"), list):
        return result["items"]
    data = result.get("data")
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    return None


def _keep_by_static_date(result: dict, date_key: str, cutoff: date) -> None:
    items = _items_of(result)
    if items is None:
        return
    kept = []
    for rec in items:
        if isinstance(rec, dict):
            d = _parse_date(rec.get(date_key)) if date_key in rec else None
            if d is not None and d > cutoff:
                continue
        kept.append(rec)
    items[:] = kept


def _mask_keys(result: dict, keys: tuple[str, ...]) -> None:
    items = _items_of(result)
    if items is None:
        return
    for rec in items:
        if not isinstance(rec, dict):
            continue
        masked = False
        for k in keys:
            if k in rec and rec[k] is not None:
                rec[k] = None
                masked = True
        if masked:
            rec["_pit_note"] = _PIT_MASK_NOTE


@_mcp.tool()
def fund_basic_info(as_of_date: str, fund_codes: list[str]) -> dict[str, Any]:
    """基金基本信息（时点版）：基金公司/经理/类型/成立时间。

    注意: 成立时间晚于 as_of_date 的基金会被剔除; "基金经理"/"最新定期报告时间" 是当前快照,
    已置 null (本服务不提供 as_of_date 当时的真实任职经理)。
    """
    cutoff, err = _check_as_of(as_of_date)
    if err:
        return err
    try:
        result = _post("/api/fund/basic-info", {"fund_codes": fund_codes})
        if isinstance(result, dict) and "error" not in result:
            _keep_by_static_date(result, "成立时间", cutoff)
            _mask_keys(result, ("基金经理", "最新定期报告时间"))
        return _pit_wrap(result, cutoff, as_of_date, generic_filter=False)
    except Exception as e:
        return _err(e)


@_mcp.tool()
def stock_profile(as_of_date: str, stock_codes: list[str]) -> dict[str, Any]:
    """股票画像（时点版）：基础信息、申万行业、中信行业。

    注意: 上市日期晚于 as_of_date 的股票会被剔除; 行业分类为当前值 (变动较慢, 未做屏蔽)。
    """
    cutoff, err = _check_as_of(as_of_date)
    if err:
        return err
    try:
        result = _post("/api/stock/profile", {"stock_codes": stock_codes})
        if isinstance(result, dict) and "error" not in result:
            _keep_by_static_date(result, "上市日期", cutoff)
        return _pit_wrap(result, cutoff, as_of_date, generic_filter=False)
    except Exception as e:
        return _err(e)
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `cd /home/rooot/agent_invest_lab && python3 -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/rooot/agent_invest_lab
git add ttjj_data_pit_mcp.py tests/test_tools.py
git commit -m "feat: add fund_basic_info & stock_profile with point-in-time masking"
```

---

## Task 9: `ttjj_research_search` (window recompute) and `market_realtime_quote` (generic filter)

**Files:**
- Modify: `/home/rooot/agent_invest_lab/ttjj_data_pit_mcp.py` (add 2 tools)
- Modify: `/home/rooot/agent_invest_lab/tests/test_tools.py` (add a test class)

- `ttjj_research_search`: upstream takes `search_days` ("近 N 天"). We can't pass an explicit end date, so we both (a) recompute nothing on the request side beyond keeping `search_days` (the upstream "近 N 天" is relative to *its* today, which we can't change) — instead we just call upstream then (b) run the generic `_filter_response`, which drops any returned article whose publish-date field (e.g. `发布时间` / `日期` / `publish_time`) is > cutoff. Additionally we add a `_pit_note` to the result explaining the search window may have included (now-removed) future articles. Keep the same params as `server.py`, plus `as_of_date` first.
- `market_realtime_quote`: just `as_of_date` first + generic `_filter_response` (drops any quote record whose timestamp field is > cutoff). Document the "effectively today-only" limitation in the docstring.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_tools.py`)

```python
class TestSpecialTools:
    def test_research_search_filters_future_articles(self, monkeypatch):
        def fake(path, data):
            assert path == "/api/research/search"
            assert data["search_type"] == "news"
            return {"success": True, "results": [
                {"标题": "旧闻", "发布时间": "2023-12-20 10:00:00"},
                {"标题": "未来新闻", "发布时间": "2026-05-01 10:00:00"},
            ]}
        monkeypatch.setattr(m, "_post", fake)
        out = m.ttjj_research_search("2024-01-01", "贵州茅台")
        assert [r["标题"] for r in out["results"]] == ["旧闻"]
        assert out["_as_of_date"] == "2024-01-01"
        assert "_pit_note" in out

    def test_research_search_bad_as_of(self, monkeypatch):
        monkeypatch.setattr(m, "_post", lambda *a, **k: {"success": True})
        assert m.ttjj_research_search("xx", "q") == {"error": "bad_as_of_date", "message": "xx"}

    def test_realtime_quote_filters_future_timestamps(self, monkeypatch):
        def fake(path, data):
            assert path == "/api/market/realtime-quote"
            return {"success": True, "items": [
                {"代码": "600519", "时间": "2023-12-29 15:00:00", "最新价": 1700.0},
                {"代码": "000300", "时间": "2026-05-11 15:00:00", "最新价": 3500.0},
            ]}
        monkeypatch.setattr(m, "_post", fake)
        out = m.market_realtime_quote("2024-01-01", ["600519", "000300"])
        assert [r["代码"] for r in out["items"]] == ["600519"]
        assert out["_as_of_date"] == "2024-01-01"

    def test_realtime_quote_bad_as_of(self, monkeypatch):
        monkeypatch.setattr(m, "_post", lambda *a, **k: {"success": True})
        assert m.market_realtime_quote("xx", ["1"]) == {"error": "bad_as_of_date", "message": "xx"}

    def test_special_tools_registered(self):
        assert callable(m.ttjj_research_search)
        assert callable(m.market_realtime_quote)
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `cd /home/rooot/agent_invest_lab && python3 -m pytest tests/test_tools.py::TestSpecialTools -q`
Expected: FAIL — `AttributeError: ... has no attribute 'ttjj_research_search'`

- [ ] **Step 3: Implement the 2 tools** (append to `ttjj_data_pit_mcp.py`)

```python
# ===========================================================================
#  研报 / 实时行情 — 特殊处理
# ===========================================================================

@_mcp.tool()
def ttjj_research_search(
    as_of_date: str,
    query: str,
    search_type: str = "news",
    top_k: int = 5,
    search_days: int = 7,
    score_threshold: float = 0.3,
) -> dict[str, Any]:
    """天天基金研报/资讯搜索（时点版）。search_type: news/research/all。

    注意: 上游的 "近 search_days 天" 是相对它的当前时间, 无法改成相对 as_of_date;
    本服务在拿到结果后会把发布时间晚于 as_of_date 的条目剔除, 所以历史 as_of_date 下
    返回的条数可能远少于 top_k(甚至为空)。
    """
    cutoff, err = _check_as_of(as_of_date)
    if err:
        return err
    try:
        result = _post("/api/research/search", {
            "query": query,
            "search_type": search_type,
            "top_k": top_k,
            "search_days": search_days,
            "score_threshold": score_threshold,
        })
        result = _pit_wrap(result, cutoff, as_of_date)
        if isinstance(result, dict) and "error" not in result:
            result["_pit_note"] = ("搜索窗口为相对上游当前时间的近 N 天; 发布时间晚于 as_of_date "
                                   "的条目已被剔除, 结果数可能少于 top_k。")
        return result
    except Exception as e:
        return _err(e)


@_mcp.tool()
def market_realtime_quote(
    as_of_date: str,
    codes: list[str],
    include: Optional[list[str]] = None,
    raw: bool = False,
    timeout: int = 10,
) -> dict[str, Any]:
    """实时行情（时点版）。codes: 6位个股代码 或 secid(如 1.000300=沪深300)。

    include: quote/order_book/capital_flow/valuation/industry/index

    注意: 实时行情的时间戳必然是"现在"; 当 as_of_date 早于今天时, 返回的行情会被全部
    过滤掉(即该工具实际只对"今天"有效)。
    """
    cutoff, err = _check_as_of(as_of_date)
    if err:
        return err
    try:
        d: dict = {"codes": codes, "raw": raw, "timeout": timeout}
        if include:
            d["include"] = include
        return _pit_wrap(_post("/api/market/realtime-quote", d), cutoff, as_of_date)
    except Exception as e:
        return _err(e)
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `cd /home/rooot/agent_invest_lab && python3 -m pytest -q`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
cd /home/rooot/agent_invest_lab
git add ttjj_data_pit_mcp.py tests/test_tools.py
git commit -m "feat: add ttjj_research_search & market_realtime_quote point-in-time tools"
```

---

## Task 10: `main()` entrypoint + `restart.sh` + tool-set guard test

**Files:**
- Modify: `/home/rooot/agent_invest_lab/ttjj_data_pit_mcp.py` (add `main()` + `if __name__` block)
- Create: `/home/rooot/agent_invest_lab/restart.sh`
- Modify: `/home/rooot/agent_invest_lab/tests/test_tools.py` (add the "dropped tools absent" guard)

- [ ] **Step 1: Write the failing test** (append to `tests/test_tools.py`)

```python
def test_dropped_tools_are_absent():
    dropped = [
        "fund_select", "fund_performance", "fund_manager_profile", "fund_style_analysis",
        "fund_rate", "fund_theme_screening", "fund_stock_holdings_screen", "fund_index_tracking",
        "fund_top_holdings", "fund_invest_position", "fund_turnover_rate", "fund_industry_exposure",
        "entity_extract", "health_check",
    ]
    for name in dropped:
        assert not hasattr(m, name), name


def test_main_callable():
    assert callable(m.main)
```

- [ ] **Step 2: Run the test, verify the `test_main_callable` part fails**

Run: `cd /home/rooot/agent_invest_lab && python3 -m pytest tests/test_tools.py::test_main_callable -q`
Expected: FAIL — `AttributeError: module 'ttjj_data_pit_mcp' has no attribute 'main'`
(The `test_dropped_tools_are_absent` test should already pass — none of those names were ever defined.)

- [ ] **Step 3: Implement `main()`** (append to the bottom of `ttjj_data_pit_mcp.py`)

```python
# ===========================================================================
#  Entrypoint
# ===========================================================================

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18078)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    _mcp.settings.host = args.host
    _mcp.settings.port = args.port
    _mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Write `restart.sh`** (`/home/rooot/agent_invest_lab/restart.sh`)

```bash
#!/usr/bin/env bash
set -euo pipefail

PORT=18078
DIR="$(cd "$(dirname "$0")" && pwd)"

lsof -ti:"$PORT" | xargs -r kill 2>/dev/null || true
sleep 0.5

nohup python3 "$DIR/ttjj_data_pit_mcp.py" --port "$PORT" > /tmp/ttjj-data-pit-mcp.log 2>&1 &
echo "ttjj-data-pit-mcp started on :$PORT (pid $!)"
```

Then: `chmod +x /home/rooot/agent_invest_lab/restart.sh`

- [ ] **Step 5: Run the full test suite, verify it passes**

Run: `cd /home/rooot/agent_invest_lab && python3 -m pytest -q`
Expected: PASS — all tests.

- [ ] **Step 6: Commit**

```bash
cd /home/rooot/agent_invest_lab
git add ttjj_data_pit_mcp.py restart.sh tests/test_tools.py
git commit -m "feat: add main() entrypoint and restart.sh; guard dropped tools"
```

---

## Task 11: Live smoke test + final verification

**Files:**
- Create: `/home/rooot/agent_invest_lab/tests/test_smoke.py`

- [ ] **Step 1: Write the opt-in live smoke test** (`tests/test_smoke.py`, new file)

```python
import os
from datetime import date

import pytest

import ttjj_data_pit_mcp as m

pytestmark = pytest.mark.skipif(
    os.environ.get("TTJJ_PIT_LIVE") != "1",
    reason="set TTJJ_PIT_LIVE=1 to run live upstream smoke tests",
)


def _all_dates_le(obj, cutoff):
    """Assert no recognized date-field value anywhere in obj is > cutoff."""
    if isinstance(obj, list):
        for el in obj:
            _all_dates_le(el, cutoff)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if m._is_date_field(k):
                d = m._parse_date(v)
                assert d is None or d <= cutoff, f"{k}={v} > {cutoff}"
            else:
                _all_dates_le(v, cutoff)


def test_fund_nav_live_respects_as_of():
    cutoff = date(2024, 1, 1)
    out = m.fund_nav("2024-01-01", ["110011"])
    assert out.get("_as_of_date") == "2024-01-01"
    assert "error" not in out, out
    _all_dates_le(out, cutoff)


def test_fund_nav_live_clamps_future_end_date():
    out = m.fund_nav("2024-01-01", ["110011"], end_date="2025-12-31")
    assert "error" not in out, out
    _all_dates_le(out, date(2024, 1, 1))


def test_stock_ownership_live_rejects_future_report_date():
    out = m.stock_ownership("2024-01-01", ["600519"], report_date="2025-12-31")
    assert out["error"] == "lookahead"
```

- [ ] **Step 2: Run the offline suite (smoke skipped)**

Run: `cd /home/rooot/agent_invest_lab && python3 -m pytest -q`
Expected: PASS, with `tests/test_smoke.py` showing as skipped.

- [ ] **Step 3: Run the live smoke test**

Run: `cd /home/rooot/agent_invest_lab && TTJJ_PIT_LIVE=1 python3 -m pytest tests/test_smoke.py -q`
Expected: PASS (requires network access to `ttjj-data-api.jijinmima.cn`). If the upstream is unreachable, note it and move on — the offline suite is the gate.

- [ ] **Step 4: Start the server and sanity-check it boots**

Run: `bash /home/rooot/agent_invest_lab/restart.sh && sleep 2 && tail -n 20 /tmp/ttjj-data-pit-mcp.log`
Expected: log shows FastMCP starting the streamable-http server on `127.0.0.1:18078`, no traceback. (Leave it running or `lsof -ti:18078 | xargs -r kill` to stop — your call.)

- [ ] **Step 5: Final self-verification checklist (manual)**

Confirm:
- `python3 -m pytest -q` → all pass.
- `grep -nE "@_mcp.tool" ttjj_data_pit_mcp.py | wc -l` → `19`.
- `grep -nE "def (fund_select|fund_performance|fund_manager_profile|fund_style_analysis|fund_rate|fund_theme_screening|fund_stock_holdings_screen|fund_index_tracking|fund_top_holdings|fund_invest_position|fund_turnover_rate|fund_industry_exposure|entity_extract|health_check)\b" ttjj_data_pit_mcp.py` → no matches.
- Every tool's first parameter is `as_of_date: str`.
- The original `/home/rooot/MCP/ttjj-data-mcp/` is unchanged (`cd /home/rooot/MCP/ttjj-data-mcp && git status` if it's a repo, or just confirm mtimes).

- [ ] **Step 6: Commit**

```bash
cd /home/rooot/agent_invest_lab
git add tests/test_smoke.py
git commit -m "test: add opt-in live upstream smoke tests"
```

---

## Notes for the implementer

- Append code to `ttjj_data_pit_mcp.py` in task order; the final file layout (top → bottom) is: module docstring → imports → constants → `_parse_date` → `_is_date_field` (+ `_DATE_FIELD_RE` widening from Task 3) → `_record_has_future_date` / `_filter_response` → `_check_as_of` / `_clamp_end_date` / `_reject_if_future` → `_get_session` / `_post` / `_pit_wrap` / `_err` → the 15 dated tools → `_items_of` / `_keep_by_static_date` / `_mask_keys` / `fund_basic_info` / `stock_profile` → `ttjj_research_search` / `market_realtime_quote` → `main()` → `if __name__ == "__main__"`. (Helpers used by tools must be defined above the tools; `_items_of` etc. can go just before Task 8's tools.)
- `pytest` imports `ttjj_data_pit_mcp` as a top-level module — run pytest from `/home/rooot/agent_invest_lab/` so the module is on `sys.path`. If discovery fails, add an empty `tests/__init__.py` or a `conftest.py` at repo root with `import sys, os; sys.path.insert(0, os.path.dirname(__file__))`.
- Do not edit anything under `/home/rooot/MCP/`.
- If `python3 -m pip install` is blocked (no network), check whether `mcp` / `requests` / `pytest` are already importable (`python3 -c "import mcp, requests, pytest"`) before failing the task — the sibling MCP project already uses them.
