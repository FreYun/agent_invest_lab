"""S9 — DB 持久化: s9_candidates (进看板) + s9_select_runs (全量 JSON)."""

from __future__ import annotations

import json
import logging

from strategy_common import get_market_db, init_market_db


S9_TABLE_DDL = [
    """
    CREATE TABLE IF NOT EXISTS s9_candidates (
        date            TEXT NOT NULL,
        code            TEXT NOT NULL,
        name            TEXT,
        industry        TEXT,
        sleeve          TEXT,
        sleeve_label    TEXT,
        factor_name     TEXT,
        factor_value    REAL,
        rank            INTEGER,
        close           REAL,
        pct_chg         REAL,
        total_mv_yi     REAL,
        circ_mv_yi      REAL,
        turnover_rate   REAL,
        pe_ttm          REAL,
        amount          REAL,
        regime          TEXT,
        regime_signal   REAL,
        reason          TEXT,
        created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (date, code)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_s9_cand_date ON s9_candidates(date);",
    "CREATE INDEX IF NOT EXISTS idx_s9_cand_code ON s9_candidates(code);",
    """
    CREATE TABLE IF NOT EXISTS s9_select_runs (
        date            TEXT PRIMARY KEY,
        strategy        TEXT NOT NULL,
        regime          TEXT,
        sleeve          TEXT,
        regime_signal   REAL,
        ma_window       INTEGER,
        index_code      TEXT,
        kept_count      INTEGER,
        universe_size   INTEGER,
        payload_json    TEXT,
        created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
]


def ensure_s9_tables(conn) -> None:
    for stmt in S9_TABLE_DDL:
        conn.execute(stmt)


def write_select_run(payload: dict) -> None:
    init_market_db()
    conn = get_market_db()
    try:
        ensure_s9_tables(conn)
        date = payload["date"]
        regime = payload.get("regime") or {}
        stats = payload.get("stats") or {}

        conn.execute("DELETE FROM s9_candidates WHERE date=?", (date,))
        for c in payload.get("candidates") or []:
            _insert_candidate(conn, date, c, regime)

        conn.execute(
            """
            INSERT OR REPLACE INTO s9_select_runs (
                date, strategy, regime, sleeve, regime_signal,
                ma_window, index_code, kept_count, universe_size, payload_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                date,
                payload.get("strategy", "S9"),
                regime.get("regime"),
                regime.get("sleeve"),
                regime.get("signal"),
                regime.get("ma_window"),
                regime.get("index_code"),
                stats.get("kept_count"),
                stats.get("universe_size"),
                json.dumps(payload, ensure_ascii=False, default=str),
            ),
        )
        conn.commit()
        logging.info(
            "DB 写入 S9: date=%s regime=%s sleeve=%s 进看板=%s",
            date, regime.get("regime"), regime.get("sleeve"),
            len(payload.get("candidates") or []),
        )
    finally:
        conn.close()


def _insert_candidate(conn, date: str, c: dict, regime: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO s9_candidates (
            date, code, name, industry, sleeve, sleeve_label,
            factor_name, factor_value, rank, close, pct_chg,
            total_mv_yi, circ_mv_yi, turnover_rate, pe_ttm, amount,
            regime, regime_signal, reason
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            date, c["code"], c.get("name"), c.get("industry"),
            c.get("sleeve"), c.get("sleeve_label"),
            c.get("factor_name"), c.get("factor"), c.get("rank"),
            c.get("close"), c.get("pct_chg"),
            c.get("total_mv_yi"), c.get("circ_mv_yi"),
            c.get("turnover_rate"), c.get("pe_ttm"), c.get("amount"),
            regime.get("regime"), regime.get("signal"),
            c.get("reason"),
        ),
    )


def read_select_run(date: str) -> dict | None:
    init_market_db()
    conn = get_market_db()
    try:
        ensure_s9_tables(conn)
        r = conn.execute("SELECT payload_json FROM s9_select_runs WHERE date=?", (date,)).fetchone()
        return json.loads(r["payload_json"]) if r and r["payload_json"] else None
    finally:
        conn.close()
