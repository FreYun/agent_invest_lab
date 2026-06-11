#!/usr/bin/env python3
"""Backfill fund_nav from ttjj REST API with throttling and resumability.

Default behavior:
- Read fund codes from fund_info.
- For each fund, start from max(existing nav_date + 1 day, --start-date).
- Fetch NAV through --end-date, default today.
- Write only fund_nav; do not touch fund_info or performance tables.

The ttjj /api/fund/nav endpoint supports multiple fund_codes. To keep a single
request bounded, this script groups only funds with the same effective date
window and caps request batch size.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests


DB_PATH = "/home/rooot/agent_invest_lab/data/fund.db"
LOG_PATH = "/home/rooot/agent_invest_lab/logs/fund-backfill-nav-ttjj.log"
FAIL_LOG_PATH = "/home/rooot/agent_invest_lab/logs/fund-backfill-nav-ttjj-failures.json"
TTJJ_API_URL = "http://ttjj-data-api.jijinmima.cn"
API_TIMEOUT = 120
API_HEADERS = {"Content-Type": "application/json"}

Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("fund-backfill-nav-ttjj")
log.setLevel(logging.INFO)
if not log.handlers:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.handlers = [fh, sh]
    log.propagate = False


def normalize_date(value: str) -> str:
    value = value.strip()
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    datetime.strptime(value, "%Y-%m-%d")
    return value


def next_day(value: str) -> str:
    d = datetime.strptime(value, "%Y-%m-%d").date()
    return (d + timedelta(days=1)).isoformat()


def parse_float(value: Any) -> float | None:
    if value in (None, "", "--"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def chunked(seq: list[str], size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def read_codes(conn: sqlite3.Connection, codes: list[str] | None, limit: int | None) -> list[str]:
    if codes:
        out = [str(c).strip().zfill(6) for c in codes if str(c).strip()]
    else:
        rows = conn.execute(
            "SELECT fund_code FROM fund_info WHERE fund_code IS NOT NULL AND fund_code != '' ORDER BY fund_code"
        ).fetchall()
        out = [r["fund_code"] for r in rows]
    if limit is not None:
        out = out[:limit]
    return out


def latest_nav_dates(conn: sqlite3.Connection, codes: list[str]) -> dict[str, str]:
    if not codes:
        return {}
    latest: dict[str, str] = {}
    for batch in chunked(codes, 900):
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT fund_code, MAX(nav_date) AS d FROM fund_nav "
            f"WHERE fund_code IN ({placeholders}) GROUP BY fund_code",
            batch,
        ).fetchall()
        latest.update({r["fund_code"]: r["d"] for r in rows if r["d"]})
    return latest


def build_work_items(
    conn: sqlite3.Connection,
    codes: list[str],
    start_date: str,
    end_date: str,
    force: bool,
) -> list[tuple[str, str, str]]:
    latest = latest_nav_dates(conn, codes)
    items: list[tuple[str, str, str]] = []
    for code in codes:
        effective_start = start_date
        if not force and latest.get(code):
            effective_start = max(start_date, next_day(latest[code]))
        if effective_start <= end_date:
            items.append((code, effective_start, end_date))
    return items


def ttjj_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = requests.post(
        f"{TTJJ_API_URL}{path}",
        json=payload,
        headers=API_HEADERS,
        timeout=API_TIMEOUT,
    )
    resp.encoding = "utf-8"
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("success") is False:
        raise ValueError(data.get("message") or f"ttjj-api {path} returned success=false")
    return data


def fetch_nav_batch(codes: list[str], start_date: str, end_date: str) -> dict[str, list[dict[str, Any]]]:
    data = ttjj_post(
        "/api/fund/nav",
        {"fund_codes": codes, "start_date": start_date, "end_date": end_date},
    )
    out: dict[str, list[dict[str, Any]]] = {code: [] for code in codes}
    for item in data.get("items") or []:
        code = str(item.get("基金代码") or "").strip().zfill(6)
        if code:
            out[code] = item.get("净值记录") or []
    return out


def normalize_nav_rows(records_by_code: dict[str, list[dict[str, Any]]]) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for code, records in records_by_code.items():
        for rec in records:
            if not isinstance(rec, dict):
                continue
            nav_date = rec.get("交易日期")
            if nav_date and len(str(nav_date)) == 8 and str(nav_date).isdigit():
                s = str(nav_date)
                nav_date = f"{s[:4]}-{s[4:6]}-{s[6:]}"
            if not nav_date:
                continue
            rows.append(
                (
                    code,
                    nav_date,
                    parse_float(rec.get("复权单位净值")),
                    None,
                    parse_float(rec.get("日收益率")),
                    now,
                )
            )
    return rows


def upsert_nav_rows(conn: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> int:
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT OR REPLACE INTO fund_nav
          (fund_code, nav_date, nav, acc_nav, daily_return_pct, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return len(rows)


def grouped_batches(items: list[tuple[str, str, str]], api_batch_size: int):
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for code, start_date, end_date in items:
        grouped[(start_date, end_date)].append(code)
    for (start_date, end_date), codes in sorted(grouped.items()):
        for batch in chunked(codes, api_batch_size):
            yield batch, start_date, end_date


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", default="2024-01-01", help="Backfill floor date, default 2024-01-01")
    parser.add_argument("--end-date", default=date.today().isoformat(), help="End date, default today")
    parser.add_argument("--codes", nargs="*", help="Optional fund codes; default reads all fund_info codes")
    parser.add_argument("--limit", type=int, help="Only process first N codes, for testing")
    parser.add_argument("--api-batch-size", type=int, default=5, help="Max fund codes per /api/fund/nav request")
    parser.add_argument("--sleep", type=float, default=0.8, help="Seconds to sleep between API requests")
    parser.add_argument("--commit-every", type=int, default=20, help="Commit after this many API requests")
    parser.add_argument("--force", action="store_true", help="Refetch from --start-date even if nav already exists")
    args = parser.parse_args()

    start_date = normalize_date(args.start_date)
    end_date = normalize_date(args.end_date)
    if start_date > end_date:
        parser.error("--start-date cannot be later than --end-date")
    if args.api_batch_size < 1:
        parser.error("--api-batch-size must be positive")
    if args.commit_every < 1:
        parser.error("--commit-every must be positive")

    conn = get_conn()
    codes = read_codes(conn, args.codes, args.limit)
    items = build_work_items(conn, codes, start_date, end_date, args.force)
    batches = list(grouped_batches(items, args.api_batch_size))

    log.info(
        "start codes=%d work_items=%d api_batches=%d window_floor=%s end=%s batch_size=%d sleep=%.2fs force=%s",
        len(codes), len(items), len(batches), start_date, end_date, args.api_batch_size, args.sleep, args.force,
    )
    if not batches:
        log.info("nothing to backfill")
        conn.close()
        Path(FAIL_LOG_PATH).write_text("[]", encoding="utf-8")
        return 0

    total_rows = 0
    failed: list[dict[str, Any]] = []
    pending_requests = 0
    started = time.time()
    try:
        for idx, (batch_codes, batch_start, batch_end) in enumerate(batches, start=1):
            try:
                records = fetch_nav_batch(batch_codes, batch_start, batch_end)
                rows = normalize_nav_rows(records)
                written = upsert_nav_rows(conn, rows)
                total_rows += written
                pending_requests += 1
                log.info(
                    "[%d/%d] codes=%s window=%s..%s rows=%d total_rows=%d",
                    idx, len(batches), ",".join(batch_codes), batch_start, batch_end, written, total_rows,
                )
            except Exception as exc:
                failed.append(
                    {
                        "fund_codes": batch_codes,
                        "start_date": batch_start,
                        "end_date": batch_end,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                log.error(
                    "[%d/%d] failed codes=%s window=%s..%s: %s",
                    idx, len(batches), ",".join(batch_codes), batch_start, batch_end, exc,
                )

            if pending_requests >= args.commit_every:
                conn.commit()
                pending_requests = 0

            if idx < len(batches):
                time.sleep(args.sleep)
    finally:
        conn.commit()
        conn.close()

    Path(FAIL_LOG_PATH).write_text(json.dumps(failed, ensure_ascii=False, indent=2), encoding="utf-8")
    elapsed = time.time() - started
    log.info(
        "done codes=%d work_items=%d api_batches=%d rows=%d failed_batches=%d elapsed_sec=%.1f fail_log=%s",
        len(codes), len(items), len(batches), total_rows, len(failed), elapsed, FAIL_LOG_PATH,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
