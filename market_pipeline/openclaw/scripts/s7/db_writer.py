"""S7 DB persistence helpers."""

from __future__ import annotations

import json
import logging
from typing import Optional

from strategy_common import get_market_db, init_market_db, is_st, resolve_names


S7_TABLE_DDL = [
    """
    CREATE TABLE IF NOT EXISTS s7_select_runs (
        date                      TEXT PRIMARY KEY,
        strategy                  TEXT NOT NULL,
        regime_code               TEXT,
        regime_name               TEXT,
        regime_score              INTEGER,
        confidence                TEXT,
        switched                  INTEGER,
        emergency_switch          INTEGER,
        position_limit_single_base REAL,
        mode                      TEXT,
        skipped_reason            TEXT,
        regime_gate_allowed       INTEGER,
        regime_gate_reason        TEXT,
        universe_size             INTEGER,
        oversold_pool_size        INTEGER,
        passed_count              INTEGER,
        config_json               TEXT,
        created_at                TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS s7_candidates (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        date              TEXT NOT NULL,
        code              TEXT NOT NULL,
        name              TEXT,
        industry          TEXT,
        mode              TEXT,
        ma20              REAL,
        dist_ma20_pct     REAL,
        ret5_pct          REAL,
        amount_yi         REAL,
        pct_chg           REAL,
        t_open            REAL,
        t_high            REAL,
        t_low             REAL,
        t_close           REAL,
        t_pct_chg         REAL,
        entry_zone_low    REAL NOT NULL,
        entry_zone_high   REAL NOT NULL,
        entry_rule        TEXT,
        stop_loss_price   REAL NOT NULL,
        stop_loss_rule    TEXT,
        take_profit_price REAL,
        take_profit_rule  TEXT,
        exit_rule         TEXT,
        position_pct      REAL NOT NULL,
        position_calc     TEXT,
        signal_score      REAL,
        rank              INTEGER,
        UNIQUE(date, code)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_s7_cand_date ON s7_candidates(date);",
    "CREATE INDEX IF NOT EXISTS idx_s7_cand_code ON s7_candidates(code);",
    """
    CREATE TABLE IF NOT EXISTS s7_candidate_rejects (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        date           TEXT NOT NULL,
        code           TEXT NOT NULL,
        name           TEXT,
        stage_failed   TEXT NOT NULL,
        reject_reason  TEXT NOT NULL,
        UNIQUE(date, code)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_s7_rej_date ON s7_candidate_rejects(date);",
    """
    CREATE TABLE IF NOT EXISTS s7_verifications (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        t_date            TEXT NOT NULL,
        t1_date           TEXT NOT NULL,
        candidate_id      INTEGER NOT NULL,
        code              TEXT NOT NULL,
        mode              TEXT NOT NULL,
        status            TEXT NOT NULL,
        entry_status      TEXT,
        entry_price       REAL,
        exit_price        REAL,
        exit_reason       TEXT,
        pnl_pct           REAL,
        t1_open           REAL,
        t1_high           REAL,
        t1_low            REAL,
        t1_close          REAL,
        t1_open_gap_pct   REAL,
        live_open_price   REAL,
        live_current      REAL,
        note              TEXT,
        hold_days         INTEGER,
        exit_date         TEXT,
        created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(candidate_id) REFERENCES s7_candidates(id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_s7_veri_t1 ON s7_verifications(t1_date);",
    "CREATE INDEX IF NOT EXISTS idx_s7_veri_code ON s7_verifications(code);",
    "CREATE INDEX IF NOT EXISTS idx_s7_veri_mode ON s7_verifications(mode);",
]


def ensure_s7_tables(conn) -> None:
    for stmt in S7_TABLE_DDL:
        conn.execute(stmt)


def write_select_run(payload: dict) -> None:
    init_market_db()
    conn = get_market_db()
    try:
        ensure_s7_tables(conn)
        date = payload["date"]
        regime = payload.get("regime_input", {})
        obs = payload.get("regime_observation") or {}
        stats = payload.get("stats") or {}
        conn.execute(
            """
            INSERT OR REPLACE INTO s7_select_runs (
                date, strategy, regime_code, regime_name, regime_score,
                confidence, switched, emergency_switch, position_limit_single_base, mode,
                skipped_reason, regime_gate_allowed, regime_gate_reason,
                universe_size, oversold_pool_size, passed_count, config_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                date,
                payload.get("strategy", "S7"),
                regime.get("code", ""),
                regime.get("regime_name", ""),
                regime.get("score", 0) or 0,
                regime.get("confidence"),
                1 if regime.get("switched") else 0,
                1 if regime.get("emergency_switch") else 0,
                regime.get("position_limit_single_base"),
                payload.get("mode"),
                payload.get("skipped_reason"),
                None,
                obs.get("note"),
                stats.get("universe_size"),
                stats.get("oversold_pool_size"),
                stats.get("passed_count"),
                json.dumps(payload.get("config") or {}, ensure_ascii=False),
            ),
        )
        conn.execute("DELETE FROM s7_verifications WHERE t_date = ?", (date,))
        conn.execute("DELETE FROM s7_candidates WHERE date = ?", (date,))
        _cands = payload.get("candidates") or []
        _names = resolve_names([x.get("code") for x in _cands], as_of=date)
        for cand in _cands:
            _rn = _names.get(str(cand.get("code")).split(".")[0].zfill(6))
            if _rn:
                cand["name"] = _rn  # 回填真名(源表 name 原存代码)
            if is_st(cand.get("name")):
                continue
            _insert_candidate(conn, date, cand)
        conn.execute("DELETE FROM s7_candidate_rejects WHERE date = ?", (date,))
        for reject in payload.get("reject_samples") or []:
            _insert_reject(conn, date, reject)
        conn.commit()
        logging.info("DB 写入 S7 select_run: date=%s candidates=%s", date, len(payload.get("candidates") or []))
    finally:
        conn.close()


def _insert_candidate(conn, date: str, c: dict) -> None:
    s = c["signal"]
    b = c["t_bar"]
    e = c["entry"]
    sl = c["stop_loss"]
    tp = c.get("take_profit") or {}
    conn.execute(
        """
        INSERT INTO s7_candidates (
            date, code, name, industry, mode,
            ma20, dist_ma20_pct, ret5_pct, amount_yi, pct_chg,
            t_open, t_high, t_low, t_close, t_pct_chg,
            entry_zone_low, entry_zone_high, entry_rule,
            stop_loss_price, stop_loss_rule,
            take_profit_price, take_profit_rule,
            exit_rule, position_pct, position_calc, signal_score, rank
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            date,
            c["code"], c.get("name"), c.get("industry"), c.get("mode"),
            s.get("ma20"), s.get("dist_ma20_pct"), s.get("ret5_pct"), s.get("amount_yi"), s.get("pct_chg"),
            b.get("open"), b.get("high"), b.get("low"), b.get("close"), b.get("pct_chg"),
            e["zone_low"], e["zone_high"], e.get("rule"),
            sl["price"], sl.get("rule"),
            tp.get("price"), tp.get("rule"),
            c.get("exit_rule"), c.get("position_pct"), c.get("position_calc"), s.get("score"), c.get("rank"),
        ),
    )


def _insert_reject(conn, date: str, reject: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO s7_candidate_rejects (date, code, name, stage_failed, reject_reason)
        VALUES (?, ?, ?, ?, ?)
        """,
        (date, reject["code"], reject.get("name"), reject["stage_failed"], reject["reject_reason"]),
    )


def write_verification(t1_date: str, t_date: str, results: list, mode: str) -> None:
    init_market_db()
    conn = get_market_db()
    try:
        ensure_s7_tables(conn)
        conn.execute("DELETE FROM s7_verifications WHERE t1_date = ? AND mode = ?", (t1_date, mode))
        for item in results:
            candidate = item.get("candidate", {})
            verification = item.get("verification", {})
            code = candidate.get("code")
            row = conn.execute("SELECT id FROM s7_candidates WHERE date = ? AND code = ?", (t_date, code)).fetchone()
            candidate_id = row["id"] if row else 0
            conn.execute(
                """
                INSERT INTO s7_verifications (
                    t_date, t1_date, candidate_id, code, mode,
                    status, entry_status, entry_price, exit_price, exit_reason, pnl_pct,
                    t1_open, t1_high, t1_low, t1_close, t1_open_gap_pct,
                    live_open_price, live_current, note, hold_days, exit_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t_date, t1_date, candidate_id, code, mode,
                    verification.get("status", "unknown"), verification.get("entry_status"),
                    verification.get("entry_price"), verification.get("exit_price"), verification.get("exit_reason"), verification.get("pnl_pct"),
                    verification.get("t1_open"), verification.get("t1_high"), verification.get("t1_low"), verification.get("t1_close"), verification.get("t1_open_gap_pct"),
                    verification.get("open_price"), verification.get("current"), verification.get("note"), verification.get("hold_days"), verification.get("exit_date"),
                ),
            )
        conn.commit()
        logging.info("DB 写入 S7 verifications: t1_date=%s rows=%s", t1_date, len(results))
    finally:
        conn.close()


def read_select_run(date: str) -> Optional[dict]:
    init_market_db()
    conn = get_market_db()
    try:
        run = conn.execute("SELECT * FROM s7_select_runs WHERE date = ?", (date,)).fetchone()
        if not run:
            return None
        cand_rows = conn.execute("SELECT * FROM s7_candidates WHERE date = ? ORDER BY signal_score DESC, code", (date,)).fetchall()
        reject_rows = conn.execute("SELECT * FROM s7_candidate_rejects WHERE date = ?", (date,)).fetchall()
    finally:
        conn.close()
    return {
        "date": run["date"],
        "strategy": run["strategy"],
        "mode": run["mode"],
        "regime_input": {
            "code": run["regime_code"],
            "regime_name": run["regime_name"],
            "score": run["regime_score"],
            "confidence": run["confidence"],
            "switched": bool(run["switched"]),
            "emergency_switch": bool(run["emergency_switch"]),
            "position_limit_single_base": run["position_limit_single_base"],
        },
        "regime_observation": {"note": run["regime_gate_reason"]},
        "candidates": [_row_to_candidate(r) for r in cand_rows],
        "reject_samples": [
            {"code": r["code"], "name": r["name"], "stage_failed": r["stage_failed"], "reject_reason": r["reject_reason"]}
            for r in reject_rows
        ],
        "skipped_reason": run["skipped_reason"],
        "stats": None if run["skipped_reason"] else {
            "universe_size": run["universe_size"],
            "oversold_pool_size": run["oversold_pool_size"],
            "passed_count": run["passed_count"],
        },
        "config": json.loads(run["config_json"]) if run["config_json"] else None,
    }


def _row_to_candidate(r) -> dict:
    return {
        "date": r["date"],
        "code": r["code"],
        "name": r["name"],
        "industry": r["industry"],
        "mode": r["mode"],
        "signal": {
            "ma20": r["ma20"],
            "dist_ma20_pct": r["dist_ma20_pct"],
            "ret5_pct": r["ret5_pct"],
            "amount_yi": r["amount_yi"],
            "pct_chg": r["pct_chg"],
            "score": r["signal_score"],
        },
        "t_bar": {
            "open": r["t_open"],
            "high": r["t_high"],
            "low": r["t_low"],
            "close": r["t_close"],
            "pct_chg": r["t_pct_chg"],
        },
        "entry": {"zone_low": r["entry_zone_low"], "zone_high": r["entry_zone_high"], "rule": r["entry_rule"]},
        "stop_loss": {"price": r["stop_loss_price"], "rule": r["stop_loss_rule"]},
        "take_profit": {"price": r["take_profit_price"], "rule": r["take_profit_rule"]},
        "exit_rule": r["exit_rule"],
        "position_pct": r["position_pct"],
        "position_calc": r["position_calc"],
        "rank": r["rank"],
    }


def read_verification(t1_date: str, mode: str | None = None) -> Optional[dict]:
    init_market_db()
    conn = get_market_db()
    try:
        if mode:
            rows = conn.execute("SELECT * FROM s7_verifications WHERE t1_date = ? AND mode = ?", (t1_date, mode)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM s7_verifications WHERE t1_date = ?", (t1_date,)).fetchall()
        if not rows:
            return None
        t_date = rows[0]["t_date"]
        cand_rows = conn.execute("SELECT * FROM s7_candidates WHERE date = ?", (t_date,)).fetchall()
        cand_by_id = {r["id"]: _row_to_candidate(r) for r in cand_rows}
    finally:
        conn.close()
    return {
        "t1_date": t1_date,
        "t_date": t_date,
        "mode": rows[0]["mode"],
        "results": [
            {
                "candidate": cand_by_id.get(r["candidate_id"], {"code": r["code"], "name": "?"}),
                "verification": {
                    "status": r["status"],
                    "entry_status": r["entry_status"],
                    "entry_price": r["entry_price"],
                    "exit_price": r["exit_price"],
                    "exit_reason": r["exit_reason"],
                    "pnl_pct": r["pnl_pct"],
                    "t1_open": r["t1_open"],
                    "t1_high": r["t1_high"],
                    "t1_low": r["t1_low"],
                    "t1_close": r["t1_close"],
                    "t1_open_gap_pct": r["t1_open_gap_pct"],
                    "open_price": r["live_open_price"],
                    "current": r["live_current"],
                    "note": r["note"],
                    "hold_days": r["hold_days"],
                    "exit_date": r["exit_date"],
                },
            }
            for r in rows
        ],
    }
