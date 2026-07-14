"""earnings_ingest — 业绩预告/快报事件拉取, 落 earnings_events 表.

设计: 详见 docs/superpowers/specs/2026-07-07-earnings-events-tracker-design.md

流程:
1. forecast 按 ann_date 循环拉 (tushare schema 强制 ann_date/ts_code 至少一个)
2. express 逐票拉 (schema 强制 ts_code): 只覆盖 forecast 已命中的 (ts_code, end_date)
3. 幂等: 主键 (ts_code, ann_date, source) + INSERT OR REPLACE
4. is_hard_hit 落库时预算(下游 SELECT WHERE is_hard_hit=1 直接用索引)

数据源:
- brze 代理 https://tu.brze.top/{api_name}, token 从 env TUSHARE_TOKEN 或 ~/.openclaw/.tushare-token
- 禁用系统代理 7897 (会劈坏国内财经 API 的 SSL)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402

TUSHARE_HTTP_URL = os.getenv("TUSHARE_HTTP_URL", "https://tu.brze.top")
DEFAULT_TIMEOUT = int(os.getenv("TUSHARE_TIMEOUT", "30"))

HARD_HIT_TYPES = frozenset(["预增", "扭亏", "略增"])
HARD_HIT_MIN_PCT = 30.0


def load_token() -> str:
    """读取 tushare token: env > ~/.openclaw/.tushare-token."""
    tok = os.getenv("TUSHARE_TOKEN")
    if tok:
        return tok.strip()
    tf = os.path.expanduser("~/.openclaw/.tushare-token")
    if os.path.exists(tf):
        with open(tf, encoding="utf-8") as f:
            t = f.read().strip()
            if t:
                return t
    raise RuntimeError(
        "未找到 tushare token: 设置 TUSHARE_TOKEN 或写入 ~/.openclaw/.tushare-token"
    )


def is_hard_hit_forecast(row: dict) -> int:
    """forecast: event_type ∈ {预增/扭亏/略增} 且 p_change_min >= 30."""
    t = row.get("type") or row.get("event_type")
    pmin = row.get("p_change_min")
    if t in HARD_HIT_TYPES and pmin is not None and pmin >= HARD_HIT_MIN_PCT:
        return 1
    return 0


def is_hard_hit_express(row: dict) -> int:
    """express: yoy_net_profit >= 30 (塞进 p_change_min 位)."""
    y = row.get("yoy_net_profit")
    if y is None:
        y = row.get("p_change_min")  # already normalized
    if y is not None and y >= HARD_HIT_MIN_PCT:
        return 1
    return 0


def parse_forecast_row(row: dict) -> dict:
    """tushare forecast row -> earnings_events 行 dict."""
    return {
        "ts_code":        row.get("ts_code"),
        "ann_date":       row.get("ann_date"),
        "source":         "forecast",
        "end_date":       row.get("end_date"),
        "event_type":     row.get("type"),
        "p_change_min":   row.get("p_change_min"),
        "p_change_max":   row.get("p_change_max"),
        "net_profit_min": row.get("net_profit_min"),
        "net_profit_max": row.get("net_profit_max"),
        "yoy_sales":      None,
        "summary":        row.get("summary"),
        "change_reason":  row.get("change_reason"),
        "is_hard_hit":    is_hard_hit_forecast(row),
    }


def parse_express_row(row: dict, ts_code: str) -> dict:
    """tushare express row -> earnings_events 行 dict."""
    yoy = row.get("yoy_net_profit")
    n_income = row.get("n_income")
    return {
        "ts_code":        ts_code,
        "ann_date":       row.get("ann_date"),
        "source":         "express",
        "end_date":       row.get("end_date"),
        "event_type":     None,
        "p_change_min":   yoy,               # yoy 塞下限位, 下游统一读
        "p_change_max":   None,
        "net_profit_min": (n_income / 10000.0) if n_income is not None else None,  # 元 -> 万元
        "net_profit_max": None,
        "yoy_sales":      row.get("yoy_sales"),
        "summary":        row.get("perf_summary"),
        "change_reason":  None,
        "is_hard_hit":    is_hard_hit_express({"yoy_net_profit": yoy}),
    }


def _no_proxy_opener():
    """禁用系统代理 7897 (会劈坏国内财经 API 的 SSL)."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call_brze(api_name: str, params: dict, fields: str, opener) -> list[dict]:
    """POST https://tu.brze.top/{api_name}, 返回 list[dict]. 失败抛 RuntimeError."""
    body = {
        "api_name": api_name,
        "token":    TOKEN,
        "params":   params or {},
        "fields":   fields or "",
    }
    data = json.dumps(body).encode("utf-8")
    url = f"{TUSHARE_HTTP_URL.rstrip('/')}/{api_name}"
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with opener.open(req, timeout=DEFAULT_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as e:
        raise RuntimeError(f"brze {api_name} 失败: {e}") from e
    if payload.get("code") != 0:
        raise RuntimeError(
            f"brze {api_name} err code={payload.get('code')} msg={payload.get('msg')!r}"
        )
    d = payload.get("data") or {}
    cols = d.get("fields") or []
    items = d.get("items") or []
    return [dict(zip(cols, row)) for row in items]


def list_trade_dates_from_daily(c: sqlite3.Connection, start: str, end: str) -> list[str]:
    """交易日历: 从 daily 表拿, YYYYMMDD 升序. trading_calendar 表空所以走 daily."""
    rows = c.execute(
        "SELECT DISTINCT trade_date FROM daily "
        "WHERE trade_date >= ? AND trade_date <= ? ORDER BY trade_date ASC",
        (start, end),
    ).fetchall()
    return [r[0] for r in rows]


def _insert_events(c: sqlite3.Connection, rows: list[dict], dry_run: bool = False) -> int:
    """INSERT OR REPLACE 批量入 earnings_events; dry_run 只算数."""
    if dry_run or not rows:
        return len(rows)
    c.executemany(
        "INSERT OR REPLACE INTO earnings_events "
        "(ts_code, ann_date, source, end_date, event_type, p_change_min, p_change_max, "
        " net_profit_min, net_profit_max, yoy_sales, summary, change_reason, is_hard_hit) "
        "VALUES (:ts_code,:ann_date,:source,:end_date,:event_type,:p_change_min,:p_change_max,"
        " :net_profit_min,:net_profit_max,:yoy_sales,:summary,:change_reason,:is_hard_hit)",
        rows,
    )
    c.commit()
    return len(rows)


FORECAST_FIELDS = ("ts_code,ann_date,end_date,type,p_change_min,p_change_max,"
                   "net_profit_min,net_profit_max,summary,change_reason")

EXPRESS_FIELDS = ("ts_code,ann_date,end_date,revenue,n_income,yoy_net_profit,"
                  "yoy_sales,perf_summary")


def _weekday_range(start: str, end: str) -> list[str]:
    """自然日历里的工作日 (周一~周五), YYYYMMDD 升序.

    forecast/express 是官方公告数据, 披露日不受"K 线已收盘"约束——公司当天盘后
    发预告, tushare 当晚就可查. 如果用 daily 表当日历(list_trade_dates_from_daily),
    会锁住到 daily.MAX(trade_date)=昨天, 拉不到今天新披露的预告. 因此 forecast
    拉取用工作日历, 非交易日 tushare 返回空自然跳过.
    """
    import datetime
    def _p(s):
        return datetime.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    out = []
    d = _p(start)
    e = _p(end)
    while d <= e:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d += datetime.timedelta(days=1)
    return out


def fetch_forecast_batch(c: sqlite3.Connection, start: str, end: str,
                         opener, dry_run: bool = False) -> int:
    """按工作日循环拉 forecast; 每日的行 parse 后批量入库. 返回入库行数."""
    dates = _weekday_range(start, end)
    if not dates:
        print(f"[ingest] forecast 窗口 {start}..{end} 无工作日, 跳过", file=sys.stderr)
        return 0
    total = 0
    for d in dates:
        try:
            raw = call_brze("forecast", {"ann_date": d}, FORECAST_FIELDS, opener)
        except RuntimeError as e:
            print(f"[ingest] forecast {d} 失败: {e}", file=sys.stderr)
            continue
        rows = [parse_forecast_row(r) for r in raw if r.get("ts_code") and r.get("ann_date")]
        n = _insert_events(c, rows, dry_run=dry_run)
        total += n
        if n:
            print(f"[ingest] forecast {d}: {n} 行")
    return total


def fetch_express_batch(c: sqlite3.Connection, opener, dry_run: bool = False) -> int:
    """按 forecast 已入库的 (ts_code, end_date) 逐票拉 express. 返回入库行数."""
    pairs = c.execute(
        "SELECT DISTINCT ts_code, end_date FROM earnings_events "
        "WHERE source='forecast' AND end_date IS NOT NULL"
    ).fetchall()
    if not pairs:
        print("[ingest] express 无 forecast 命中票, 跳过", file=sys.stderr)
        return 0
    total = 0
    for ts_code, period in pairs:
        try:
            raw = call_brze("express", {"ts_code": ts_code, "period": period},
                            EXPRESS_FIELDS, opener)
        except RuntimeError as e:
            print(f"[ingest] express {ts_code}/{period} 失败: {e}", file=sys.stderr)
            continue
        rows = [parse_express_row(r, ts_code) for r in raw if r.get("ann_date")]
        n = _insert_events(c, rows, dry_run=dry_run)
        total += n
    print(f"[ingest] express: 共 {total} 行 (from {len(pairs)} forecast 命中票)")
    return total


def _default_window(days_back: int) -> tuple[str, str]:
    """给出 [start,end] 时间字符串; end=今日, start=today - days_back (自然日, 非交易日)."""
    import datetime
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days_back)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def main() -> int:
    p = argparse.ArgumentParser(description="业绩预告/快报事件拉取")
    p.add_argument("--days-back", type=int, default=90,
                   help="forecast 拉取窗口天数 (默认 90, 覆盖 60 交易日 + buffer)")
    p.add_argument("--dry-run", action="store_true", help="只算数, 不落库")
    p.add_argument("--only-forecast", action="store_true", help="只拉 forecast")
    p.add_argument("--only-express", action="store_true", help="只拉 express")
    args = p.parse_args()

    if args.only_forecast and args.only_express:
        print("[ingest] --only-forecast 和 --only-express 互斥", file=sys.stderr)
        return 2

    start, end = _default_window(args.days_back)
    print(f"[ingest] 窗口 {start} .. {end} (days_back={args.days_back})")

    opener = _no_proxy_opener()
    c = scout_db.conn()
    try:
        nf, ne = 0, 0
        if not args.only_express:
            nf = fetch_forecast_batch(c, start, end, opener, dry_run=args.dry_run)
        if not args.only_forecast:
            ne = fetch_express_batch(c, opener, dry_run=args.dry_run)
        tag = "[dry-run] " if args.dry_run else ""
        print(f"[ingest] {tag}完成: forecast={nf}, express={ne}")
        return 0
    finally:
        c.close()


TOKEN = load_token()

if __name__ == "__main__":
    sys.exit(main())
