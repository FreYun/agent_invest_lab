"""S8 DB 持久化: s8_candidates (S7 对齐, 进看板) + s8_select_runs (全量 JSON)."""

from __future__ import annotations

import json
import logging

from strategy_common import get_market_db, init_market_db

S8_TABLE_DDL = [
    """
    CREATE TABLE IF NOT EXISTS s8_candidates (
        date              TEXT NOT NULL,
        code              TEXT NOT NULL,
        name              TEXT,
        industry          TEXT,
        play_mode         TEXT,
        tier              TEXT,
        themes            TEXT,
        pct_chg           REAL,
        net_main          REAL,
        vol_ratio         REAL,
        turnover_rate     REAL,
        circ_mv_yi        REAL,
        pe_ttm            REAL,
        dist10_pct        REAL,
        lub_streak        INTEGER,
        lub_blast         INTEGER,
        entry_zone_low    REAL,
        entry_zone_high   REAL,
        entry_rule        TEXT,
        stop_loss_price   REAL,
        stop_loss_rule    TEXT,
        take_profit_price REAL,
        take_profit_rule  TEXT,
        position_pct      REAL,
        position_calc     TEXT,
        tone              TEXT,
        regime_name       TEXT,
        reason            TEXT,
        signal_score      REAL,
        rank              INTEGER,
        created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (date, code)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_s8_cand_date ON s8_candidates(date);",
    "CREATE INDEX IF NOT EXISTS idx_s8_cand_code ON s8_candidates(code);",
    """
    CREATE TABLE IF NOT EXISTS s8_select_runs (
        date             TEXT PRIMARY KEY,
        strategy         TEXT NOT NULL,
        path             TEXT,
        tone             TEXT,
        regime_name      TEXT,
        regime_score     INTEGER,
        confidence       TEXT,
        switched         INTEGER,
        emergency_switch INTEGER,
        is_divergence    INTEGER,
        kept_count       INTEGER,
        universe_size    INTEGER,
        payload_json     TEXT,
        created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
]


def ensure_s8_tables(conn) -> None:
    for stmt in S8_TABLE_DDL:
        conn.execute(stmt)


def write_select_run(payload: dict) -> None:
    init_market_db()
    conn = get_market_db()
    try:
        ensure_s8_tables(conn)
        date = payload["date"]
        tone = payload.get("tone") or {}
        regime = payload.get("regime_input") or {}
        mainline = payload.get("mainline") or {}
        stats = payload.get("stats") or {}

        conn.execute("DELETE FROM s8_candidates WHERE date=?", (date,))
        for c in payload.get("candidates") or []:
            _insert_candidate(conn, date, c)

        conn.execute(
            """
            INSERT OR REPLACE INTO s8_select_runs (
                date, strategy, path, tone, regime_name, regime_score,
                confidence, switched, emergency_switch, is_divergence,
                kept_count, universe_size, payload_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                date,
                payload.get("strategy", "S8"),
                tone.get("path"),
                tone.get("tone"),
                regime.get("regime_name"),
                regime.get("score") or 0,
                regime.get("confidence"),
                1 if regime.get("switched") else 0,
                1 if regime.get("emergency_switch") else 0,
                1 if mainline.get("is_divergence") else 0,
                stats.get("kept_count"),
                stats.get("universe_size"),
                json.dumps(payload, ensure_ascii=False, default=str),
            ),
        )
        conn.commit()
        logging.info("DB 写入 S8: date=%s path=%s 进看板=%s",
                     date, tone.get("path"), len(payload.get("candidates") or []))
    finally:
        conn.close()


def _insert_candidate(conn, date: str, c: dict) -> None:
    industry = (c.get("themes") or ["主线"])[0]
    conn.execute(
        """
        INSERT OR REPLACE INTO s8_candidates (
            date, code, name, industry, play_mode, tier, themes,
            pct_chg, net_main, vol_ratio, turnover_rate, circ_mv_yi, pe_ttm,
            dist10_pct, lub_streak, lub_blast,
            entry_zone_low, entry_zone_high, entry_rule,
            stop_loss_price, stop_loss_rule, take_profit_price, take_profit_rule,
            position_pct, position_calc, tone, regime_name, reason, signal_score, rank
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            date, c["code"], c.get("name"), industry, c.get("play_mode"), c.get("tier"),
            json.dumps(c.get("themes") or [], ensure_ascii=False),
            c.get("pct_chg"), c.get("net_main"), c.get("vol_ratio"), c.get("turnover_rate"),
            c.get("circ_mv_yi"), c.get("pe_ttm"), c.get("dist10_pct"),
            c.get("lub_streak"), c.get("lub_blast"),
            c.get("entry_zone_low"), c.get("entry_zone_high"), c.get("entry_rule"),
            c.get("stop_loss_price"), c.get("stop_loss_rule"),
            c.get("take_profit_price"), c.get("take_profit_rule"),
            c.get("position_pct"), c.get("position_calc"),
            c.get("tone"), c.get("regime_name"), c.get("reason"),
            c.get("signal_score"), c.get("rank"),
        ),
    )


def read_select_run(date: str) -> dict | None:
    init_market_db()
    conn = get_market_db()
    try:
        ensure_s8_tables(conn)
        r = conn.execute("SELECT payload_json FROM s8_select_runs WHERE date=?", (date,)).fetchone()
        return json.loads(r["payload_json"]) if r and r["payload_json"] else None
    finally:
        conn.close()
