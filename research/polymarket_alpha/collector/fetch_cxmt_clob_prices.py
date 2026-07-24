#!/usr/bin/env /usr/bin/python3.12
"""
Fetch CXMT/ChangXin Polymarket CLOB prices into the local database.

Default behavior:
  - finds active markets whose question/slug contains "cxmt"
  - limits to IPO/market-cap markets
  - fetches midpoint, buy price, sell price, and last trade for Yes/No tokens
  - writes rich rows to clob_price_snapshot
  - also writes midpoint rows to price_history for existing history tooling

Run:
    /usr/bin/python3.12 fetch_cxmt_clob_prices.py
    /usr/bin/python3.12 fetch_cxmt_clob_prices.py --all-cxmt
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

DB = "/home/rooot/database/polymarket.db"
CLI = os.environ.get(
    "POLY_CLI", "/home/rooot/polymarket-cli-main/target/release/polymarket"
)
PROXY = os.environ.get("PM_PROXY", "http://127.0.0.1:7897")

SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS clob_price_snapshot (
    snapshot_ts       INTEGER NOT NULL,
    market_id         TEXT    NOT NULL,
    condition_id      TEXT,
    question          TEXT,
    slug              TEXT,
    token_id          TEXT    NOT NULL,
    outcome           TEXT,
    outcome_index     INTEGER,
    midpoint          REAL,
    best_bid          REAL,
    best_ask          REAL,
    last_trade_price  REAL,
    source            TEXT    NOT NULL DEFAULT 'polymarket_clob',
    seen_at           INTEGER NOT NULL,
    PRIMARY KEY (snapshot_ts, token_id)
);
"""

INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_cps_token_ts ON clob_price_snapshot(token_id, snapshot_ts)",
    "CREATE INDEX IF NOT EXISTS idx_cps_market_ts ON clob_price_snapshot(market_id, snapshot_ts)",
]


@dataclass(frozen=True)
class TokenRef:
    market_id: str
    condition_id: str | None
    question: str
    slug: str | None
    token_id: str
    outcome: str
    outcome_index: int


def _env() -> dict[str, str]:
    return {
        **os.environ,
        "HTTP_PROXY": PROXY,
        "HTTPS_PROXY": PROXY,
        "http_proxy": PROXY,
        "https_proxy": PROXY,
    }


def _run_clob(args: list[str]) -> object:
    cmd = [CLI, "-o", "json", "clob", *args]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
        env=_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"CLI failed: {' '.join(cmd)}\n{proc.stderr.strip()[:500]}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"bad JSON from CLI: {proc.stdout[:500]}") from exc


def _to_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _token_csv(tokens: list[str]) -> str:
    return ",".join(tokens)


def fetch_midpoints(tokens: list[str]) -> dict[str, float]:
    if not tokens:
        return {}
    data = _run_clob(["midpoints", _token_csv(tokens)])
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, raw in data.items() if (v := _to_float(raw)) is not None}


def fetch_side_prices(tokens: list[str], side: str) -> dict[str, float]:
    if not tokens:
        return {}
    data = _run_clob(["batch-prices", _token_csv(tokens), "--side", side])
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    side_key = side.upper()
    for token_id, payload in data.items():
        if isinstance(payload, dict):
            value = _to_float(payload.get(side_key))
            if value is not None:
                out[str(token_id)] = value
    return out


def fetch_last_trades(tokens: list[str]) -> dict[str, float]:
    if not tokens:
        return {}
    data = _run_clob(["last-trades", _token_csv(tokens)])
    out: dict[str, float] = {}
    if not isinstance(data, list):
        return out
    for row in data:
        if not isinstance(row, dict):
            continue
        token_id = row.get("token_id")
        price = _to_float(row.get("price"))
        if token_id and price is not None:
            out[str(token_id)] = price
    return out


def ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(SNAPSHOT_DDL)
    for ddl in INDEX_DDL:
        con.execute(ddl)
    con.commit()


def load_tokens(
    con: sqlite3.Connection,
    *,
    keyword: str,
    ipo_only: bool,
    include_closed: bool,
) -> list[TokenRef]:
    clauses = ["(lower(question) LIKE ? OR lower(slug) LIKE ?)"]
    params: list[object] = [f"%{keyword.lower()}%", f"%{keyword.lower()}%"]
    if ipo_only:
        clauses.append(
            "(lower(question) LIKE '%ipo%' OR lower(slug) LIKE '%ipo%' "
            "OR lower(question) LIKE '%market cap%')"
        )
    if not include_closed:
        clauses.append("closed = 0")
    sql = f"""
        SELECT market_id, condition_id, question, slug, clob_token_ids, outcomes
        FROM market
        WHERE {' AND '.join(clauses)}
        ORDER BY market_id
    """
    rows = con.execute(sql, params).fetchall()
    refs: list[TokenRef] = []
    for market_id, condition_id, question, slug, token_json, outcome_json in rows:
        try:
            token_ids = json.loads(token_json or "[]")
            outcomes = json.loads(outcome_json or "[]")
        except json.JSONDecodeError:
            continue
        for idx, token_id in enumerate(token_ids):
            refs.append(
                TokenRef(
                    market_id=str(market_id),
                    condition_id=condition_id,
                    question=question or "",
                    slug=slug,
                    token_id=str(token_id),
                    outcome=str(outcomes[idx]) if idx < len(outcomes) else str(idx),
                    outcome_index=idx,
                )
            )
    return refs


def persist(
    con: sqlite3.Connection,
    *,
    snapshot_ts: int,
    refs: list[TokenRef],
    midpoints: dict[str, float],
    bids: dict[str, float],
    asks: dict[str, float],
    last_trades: dict[str, float],
    write_price_history: bool,
) -> int:
    rows = []
    seen_at = int(time.time())
    for ref in refs:
        rows.append(
            (
                snapshot_ts,
                ref.market_id,
                ref.condition_id,
                ref.question,
                ref.slug,
                ref.token_id,
                ref.outcome,
                ref.outcome_index,
                midpoints.get(ref.token_id),
                bids.get(ref.token_id),
                asks.get(ref.token_id),
                last_trades.get(ref.token_id),
                "polymarket_clob",
                seen_at,
            )
        )
    con.executemany(
        """
        INSERT OR REPLACE INTO clob_price_snapshot VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        rows,
    )
    if write_price_history:
        con.executemany(
            "INSERT OR REPLACE INTO price_history(token_id, ts, price) VALUES (?,?,?)",
            [
                (ref.token_id, snapshot_ts, midpoints[ref.token_id])
                for ref in refs
                if ref.token_id in midpoints
            ],
        )
    con.commit()
    return len(rows)


def print_summary(
    *,
    snapshot_ts: int,
    refs: list[TokenRef],
    midpoints: dict[str, float],
    bids: dict[str, float],
    asks: dict[str, float],
    last_trades: dict[str, float],
) -> None:
    dt = datetime.fromtimestamp(snapshot_ts, tz=timezone.utc).astimezone()
    print(f"snapshot={dt.isoformat(timespec='seconds')} rows={len(refs)}")
    print(f"{'market':<8} {'outcome':<5} {'mid':>7} {'bid':>7} {'ask':>7} {'last':>7}  question")
    for ref in refs:
        mid = midpoints.get(ref.token_id)
        bid = bids.get(ref.token_id)
        ask = asks.get(ref.token_id)
        last = last_trades.get(ref.token_id)
        print(
            f"{ref.market_id:<8} {ref.outcome:<5} "
            f"{mid if mid is not None else '':>7} "
            f"{bid if bid is not None else '':>7} "
            f"{ask if ask is not None else '':>7} "
            f"{last if last is not None else '':>7}  "
            f"{ref.question[:88]}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB)
    parser.add_argument("--keyword", default="cxmt")
    parser.add_argument(
        "--all-cxmt",
        action="store_true",
        help="include non-IPO CXMT markets, e.g. Apple purchasing CXMT chips",
    )
    parser.add_argument("--include-closed", action="store_true")
    parser.add_argument("--no-price-history", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    snapshot_ts = int(time.time())
    con = sqlite3.connect(args.db, timeout=30)
    try:
        ensure_schema(con)
        refs = load_tokens(
            con,
            keyword=args.keyword,
            ipo_only=not args.all_cxmt,
            include_closed=args.include_closed,
        )
        if not refs:
            print("no matching tokens", file=sys.stderr)
            return 1

        tokens = [ref.token_id for ref in refs]
        midpoints = fetch_midpoints(tokens)
        bids = fetch_side_prices(tokens, "buy")
        asks = fetch_side_prices(tokens, "sell")
        last_trades = fetch_last_trades(tokens)

        if not args.dry_run:
            persist(
                con,
                snapshot_ts=snapshot_ts,
                refs=refs,
                midpoints=midpoints,
                bids=bids,
                asks=asks,
                last_trades=last_trades,
                write_price_history=not args.no_price_history,
            )
        print_summary(
            snapshot_ts=snapshot_ts,
            refs=refs,
            midpoints=midpoints,
            bids=bids,
            asks=asks,
            last_trades=last_trades,
        )
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
