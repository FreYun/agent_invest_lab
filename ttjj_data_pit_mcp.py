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


# ===========================================================================
#  基金基本信息 / 股票画像 — 按静态日期过滤 (bespoke)
# ===========================================================================

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
