#!/usr/bin/env python3
"""mkt_facts_fetch — 抓 GVIX / 北向 / 估值 / 利率 / 信用利差,写 4 张 mkt_*_daily 缓存表。

由 daily-regime-pipeline.sh 末尾追加调用(切换日前最后一步,失败不影响前序)。
build_fact_pack.py 读这 4 张表,bot 不再自己调 research-mcp。

设计:
  - 单工具失败 → log warning,不抛异常,不影响其他工具
  - 拉过去 300 天历史(算多年百分位用),INSERT OR REPLACE,幂等可重跑
  - 默认 trade_date = today; 也可 --date YYYY-MM-DD 回填
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

MARKET_DB_PATH = os.environ.get("MARKET_DB_PATH") or os.environ.get("SCOUT_DB_PATH") or "/home/rooot/agent_invest_lab/data/market.db"
LOG_PATH = "/home/rooot/agent_invest_lab/logs/mkt_facts_fetch.log"
RESEARCH_MCP_URL = os.getenv("RESEARCH_MCP_URL",
                              "http://research-mcp.jijinmima.cn/mcp")
HTTP_TIMEOUT = int(os.getenv("RESEARCH_MCP_TIMEOUT", "60"))
LOOKBACK_DAYS = 320  # 自然日, 保证算 250 交易日 MA 有富余

Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("mkt_facts_fetch")
log.setLevel(logging.INFO)
if not log.handlers:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.handlers = [fh, sh]
    log.propagate = False


# ============================================================
# Research MCP 客户端(streamable-http,无状态)
# ============================================================

def call_research_mcp(tool: str, args: dict, timeout: int = HTTP_TIMEOUT) -> dict | None:
    """调研 mcp 一个 tool。返回 structuredContent.data 或 None。"""
    payload = {
        "jsonrpc": "2.0",
        "id": "x",
        "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }
    req = urllib.request.Request(
        RESEARCH_MCP_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.warning("[%s] HTTP 失败: %s", tool, exc)
        return None

    # SSE 格式: "event: message\ndata: {...}\n\n"
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            body = line[5:].strip()
            if not body or body == "[DONE]":
                continue
            try:
                resp_obj = json.loads(body)
            except json.JSONDecodeError:
                continue
            result = resp_obj.get("result", {})
            if result.get("isError"):
                log.warning("[%s] tool isError, raw=%s", tool, raw[:200])
                return None
            sc = result.get("structuredContent")
            if isinstance(sc, dict) and sc.get("success"):
                return sc.get("data") or {}
            # fallback: 从 content[0].text 里解析(部分工具不返 structuredContent)
            content = result.get("content") or []
            if content and isinstance(content[0], dict):
                text = content[0].get("text") or ""
                try:
                    parsed = json.loads(text)
                    if parsed.get("success"):
                        return parsed.get("data") or {}
                except json.JSONDecodeError:
                    pass
            return None
    log.warning("[%s] SSE 响应未找到合法 data 行", tool)
    return None


# ============================================================
# 工具响应解析(都是 {columns: [...], data: [[..],..]} 形式)
# ============================================================

def _rows_from_table(data: dict) -> list[dict]:
    if not isinstance(data, dict):
        return []
    cols = data.get("columns") or []
    rows = data.get("data") or []
    return [dict(zip(cols, r)) for r in rows]


def _normalize_date(s: str) -> str:
    """'2026-05-15T00:00:00' / '20260515' / '2026-05-15' → '20260515'"""
    if not s:
        return ""
    s = str(s).split("T")[0]
    return s.replace("-", "")


def _ma(seq: list[float], window: int) -> float | None:
    if len(seq) < window:
        return None
    return sum(seq[:window]) / window


def _percentile(value: float, history: list[float]) -> float | None:
    if not history:
        return None
    lower = sum(1 for v in history if v < value)
    return lower / len(history)


# ============================================================
# 单工具抓取(每个对应一张表)
# ============================================================

def fetch_gvix(end_date: str, lookback_days: int) -> int:
    """get_ashares_gvix → mkt_gvix_daily。"""
    start = (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=lookback_days)).strftime("%Y%m%d")
    data = call_research_mcp("get_ashares_gvix", {
        "start_date": start, "end_date": end_date,
    })
    rows = _rows_from_table(data)
    if not rows:
        log.warning("[gvix] 无数据返回")
        return 0
    # 按日期升序
    parsed: list[tuple[str, float]] = []
    for r in rows:
        d = _normalize_date(r.get("交易日期") or r.get("trade_date") or "")
        v = r.get("gvix恐慌指数") or r.get("gvix") or r.get("value")
        if d and isinstance(v, (int, float)):
            parsed.append((d, float(v)))
    parsed.sort()
    # 入库: 算 ma20 + percentile_1y
    written = 0
    with sqlite3.connect(MARKET_DB_PATH) as conn:
        for i, (d, v) in enumerate(parsed):
            # 取该日(含)之前 20 个交易日
            window_ma20 = [x for _, x in parsed[max(0, i - 19):i + 1]]
            ma20 = sum(window_ma20) / len(window_ma20) if window_ma20 else None
            # 近 250 个交易日
            window_1y = [x for _, x in parsed[max(0, i - 249):i + 1]]
            pct_1y = _percentile(v, window_1y) if len(window_1y) >= 30 else None
            conn.execute(
                """INSERT OR REPLACE INTO mkt_gvix_daily
                   (trade_date, latest, ma20, percentile_1y, fetched_at)
                   VALUES (?, ?, ?, ?, datetime('now'))""",
                (d, v, ma20, pct_1y),
            )
            written += 1
        conn.commit()
    log.info("[gvix] 写入 %d 行(end_date=%s, 最新值=%s)", written, end_date, parsed[-1][1] if parsed else None)
    return written


def fetch_northbound(end_date: str, lookback_days: int) -> int:
    """[skipped] research-mcp 当前只有按 stock_code 查的北向工具, 无全市场净流入口径。

    后续若接入新数据源, 重新启用此函数。当前 fact_pack.sentiment.northbound 始终 null。
    """
    log.info("[northbound] SKIP — research-mcp 无全市场口径,fact_pack 将标 missing_dims")
    return 0
    # 以下为预留实现, 若数据源出现再启用:
    start = (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=lookback_days)).strftime("%Y%m%d")
    data = call_research_mcp("get_stock_northbound_holding", {
        "start_date": start, "end_date": end_date,
    })
    rows = _rows_from_table(data)
    if not rows:
        log.warning("[northbound] 无数据返回")
        return 0
    # 解析:可能列名是"交易日期" + "净流入"等。先打印列看一眼。
    if rows:
        log.info("[northbound] columns=%s", list(rows[0].keys())[:8])
    parsed: list[tuple[str, float]] = []
    for r in rows:
        d = _normalize_date(r.get("交易日期") or r.get("trade_date") or "")
        # 尝试多种列名
        v = (r.get("北向资金当日净流入(亿元)")
             or r.get("北向资金净流入") or r.get("北向资金净流入(亿)")
             or r.get("net_inflow") or r.get("net_inflow_yi"))
        if d and isinstance(v, (int, float)):
            parsed.append((d, float(v)))
    parsed.sort()
    if not parsed:
        log.warning("[northbound] 列名匹配失败, 全部跳过")
        return 0
    written = 0
    with sqlite3.connect(MARKET_DB_PATH) as conn:
        for i, (d, v) in enumerate(parsed):
            cum_20 = sum(x for _, x in parsed[max(0, i - 19):i + 1])
            cum_60 = sum(x for _, x in parsed[max(0, i - 59):i + 1])
            conn.execute(
                """INSERT OR REPLACE INTO mkt_northbound_daily
                   (trade_date, daily_net_inflow_yi, cum_20d_yi, cum_60d_yi, fetched_at)
                   VALUES (?, ?, ?, ?, datetime('now'))""",
                (d, v, cum_20, cum_60),
            )
            written += 1
        conn.commit()
    log.info("[northbound] 写入 %d 行(最新日净流入=%.2f 亿)", written, parsed[-1][1] if parsed else 0)
    return written


def fetch_index_val(end_date: str) -> int:
    """get_ashares_index_val(只取当日) × 3 个指数 → mkt_index_val_daily。

    工具返回结构: {ts_code: {columns: [交易日期, 市盈率_TTM, 历史百分位], data: [[...]]}}
    """
    written = 0
    target_codes = ["000300.SH", "000905.SH", "399006.SZ"]
    with sqlite3.connect(MARKET_DB_PATH) as conn:
        for ts_code in target_codes:
            data = call_research_mcp("get_ashares_index_val", {"symbol": ts_code})
            if not isinstance(data, dict):
                log.warning("[index_val] %s 无数据", ts_code)
                continue
            inner = data.get(ts_code)
            if not isinstance(inner, dict):
                log.warning("[index_val] %s 解析失败, top keys=%s", ts_code, list(data.keys())[:5])
                continue
            cols = inner.get("columns") or []
            rows = inner.get("data") or []
            if not rows:
                log.warning("[index_val] %s rows 空", ts_code)
                continue
            last_row = dict(zip(cols, rows[-1]))
            d = _normalize_date(last_row.get("交易日期", end_date))
            pe_latest = last_row.get("市盈率_TTM") or last_row.get("市盈率")
            # 工具返回 0-100 百分制, 表里存 0-1 小数
            pct_raw = last_row.get("历史百分位")
            pe_pct = (float(pct_raw) / 100.0) if isinstance(pct_raw, (int, float)) else None
            if pe_latest is None:
                log.warning("[index_val] %s pe_latest 缺失 cols=%s", ts_code, cols)
                continue
            conn.execute(
                """INSERT OR REPLACE INTO mkt_index_val_daily
                   (trade_date, ts_code, pe_latest, pe_percentile_5y, pb_latest, pb_percentile_5y, fetched_at)
                   VALUES (?, ?, ?, ?, NULL, NULL, datetime('now'))""",
                (d, ts_code, pe_latest, pe_pct),
            )
            written += 1
            log.info("[index_val] %s PE=%.2f PE_pct=%.2f", ts_code, pe_latest, pe_pct or 0)
        conn.commit()
    return written


def fetch_bond_yield(end_date: str, lookback_days: int) -> int:
    """get_cn_bond_yield + get_bond_yield_spread → mkt_bond_yield_daily。"""
    start = (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=lookback_days)).strftime("%Y%m%d")
    # 10Y 国债收益率 (工具需要 maturity 参数)
    bond_raw = call_research_mcp("get_cn_bond_yield", {
        "maturity": "10Y", "start_date": start, "end_date": end_date,
    })
    # 返回可能是嵌套 dict (按 maturity 分桶) 或者直接 columns/data
    bond_data: Any = bond_raw
    if isinstance(bond_raw, dict) and not bond_raw.get("columns"):
        # 找第一个有 columns 的子节点
        for v in bond_raw.values():
            if isinstance(v, dict) and v.get("columns"):
                bond_data = v
                break
    bond_rows = _rows_from_table(bond_data)
    if bond_rows:
        log.info("[bond_yield] columns=%s rows=%d", list(bond_rows[0].keys())[:8], len(bond_rows))
    # 信用利差 — credit_vs_cn 工具当前无数据, 跳过. 暂用 None,fact_pack 会标 missing_dims.
    # 后续若 credit_vs_cn 工具就绪, 重新启用以下代码即可:
    #   spread_raw = call_research_mcp("get_bond_yield_spread", {
    #       "spread_type": "credit_vs_cn", "maturity": "10Y",
    #       "start_date": start, "end_date": end_date,
    #   })
    log.info("[bond_spread] SKIP — credit_vs_cn 工具当前无数据,fact_pack 标 missing_dims")
    spread_data: Any = None
    spread_rows = _rows_from_table(spread_data) if spread_data is not None else []

    # 合并到 dict[trade_date] = {bond_10y, spread}
    def _pick_date(r: dict) -> str:
        return _normalize_date(r.get("日期") or r.get("交易日期") or r.get("trade_date") or "")

    def _pick_first_numeric(r: dict, *exclude_keys: str) -> float | None:
        for k, v in r.items():
            if k in exclude_keys:
                continue
            if isinstance(v, (int, float)):
                return float(v)
        return None

    by_date: dict[str, dict] = {}
    for r in bond_rows:
        d = _pick_date(r)
        # 已知列名: "中国国债10年" 等;取除日期外第一个数值列
        v = _pick_first_numeric(r, "日期", "交易日期", "trade_date")
        if d and v is not None:
            by_date.setdefault(d, {})["bond_10y"] = v
    for r in spread_rows:
        d = _pick_date(r)
        v = _pick_first_numeric(r, "日期", "交易日期", "trade_date")
        if d and v is not None:
            by_date.setdefault(d, {})["spread"] = v

    if not by_date:
        log.warning("[bond] 无数据返回")
        return 0

    sorted_dates = sorted(by_date.keys())
    bond_list = [(d, by_date[d].get("bond_10y")) for d in sorted_dates]
    spread_list = [(d, by_date[d].get("spread")) for d in sorted_dates]

    written = 0
    with sqlite3.connect(MARKET_DB_PATH) as conn:
        for i, d in enumerate(sorted_dates):
            entry = by_date[d]
            # 算 MA60 国债 / MA20 利差
            bond_window = [v for _, v in bond_list[max(0, i - 59):i + 1] if isinstance(v, (int, float))]
            spread_window = [v for _, v in spread_list[max(0, i - 19):i + 1] if isinstance(v, (int, float))]
            bond_ma60 = sum(bond_window) / len(bond_window) if bond_window else None
            spread_ma20 = sum(spread_window) / len(spread_window) if spread_window else None
            conn.execute(
                """INSERT OR REPLACE INTO mkt_bond_yield_daily
                   (trade_date, cn_bond_10y, cn_bond_10y_ma60, credit_spread, credit_spread_ma20,
                    usd_index, usd_index_ma20, fetched_at)
                   VALUES (?, ?, ?, ?, ?, NULL, NULL, datetime('now'))""",
                (d, entry.get("bond_10y"), bond_ma60, entry.get("spread"), spread_ma20),
            )
            written += 1
        conn.commit()
    log.info("[bond] 写入 %d 行", written)
    return written


# ============================================================
# main
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="结束日期 YYYY-MM-DD,默认今天")
    parser.add_argument("--lookback", type=int, default=LOOKBACK_DAYS,
                        help="历史自然日数 (默认 320, ≈250 交易日)")
    parser.add_argument("--only", choices=["gvix", "northbound", "index_val", "bond"],
                        help="只跑一个工具(调试用)")
    args = parser.parse_args()

    end_date = (args.date or datetime.now().strftime("%Y-%m-%d")).replace("-", "")
    if len(end_date) != 8 or not end_date.isdigit():
        log.error("end_date 必须是 YYYYMMDD: %s", end_date)
        return 1

    log.info("=" * 60)
    log.info("mkt_facts_fetch 开始: end_date=%s lookback=%d only=%s",
             end_date, args.lookback, args.only or "ALL")

    tasks = [
        ("gvix",       lambda: fetch_gvix(end_date, args.lookback)),
        ("northbound", lambda: fetch_northbound(end_date, args.lookback)),
        ("index_val",  lambda: fetch_index_val(end_date)),
        ("bond",       lambda: fetch_bond_yield(end_date, args.lookback)),
    ]
    if args.only:
        tasks = [t for t in tasks if t[0] == args.only]

    results = {}
    for name, fn in tasks:
        try:
            n = fn()
            results[name] = n
        except Exception as exc:
            log.exception("[%s] 异常: %s", name, exc)
            results[name] = -1

    log.info("-" * 60)
    log.info("mkt_facts_fetch 完成:")
    for name, n in results.items():
        log.info("  %s: %s 行", name, n if n >= 0 else "FAIL")
    # 全失败才算失败,部分成功不阻塞 pipeline
    return 0 if any(n > 0 for n in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
