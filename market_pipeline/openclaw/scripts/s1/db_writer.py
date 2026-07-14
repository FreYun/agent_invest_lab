"""S1 DB persistence helpers."""

from __future__ import annotations

import json
import logging
from typing import Optional

from strategy_common import ensure_regime_gate_columns, get_market_db, init_market_db, is_st, resolve_names


S1_TABLE_DDL = [
    """
    CREATE TABLE IF NOT EXISTS s1_select_runs (
        date                        TEXT PRIMARY KEY,
        strategy                    TEXT NOT NULL,

        regime_code                 TEXT NOT NULL,
        regime_name                 TEXT NOT NULL,
        regime_score                INTEGER NOT NULL,
        confidence                  TEXT,
        switched                    INTEGER,
        emergency_switch            INTEGER,
        position_limit_single_base  REAL,
        mode                        TEXT,

        skipped_reason              TEXT,
        regime_gate_allowed         INTEGER,
        regime_gate_reason          TEXT,

        universe_size               INTEGER,
        breakout_pool_size          INTEGER,
        passed_count                INTEGER,
        config_json                 TEXT,

        created_at                  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS s1_candidates (
        id                        INTEGER PRIMARY KEY AUTOINCREMENT,
        date                      TEXT NOT NULL,
        code                      TEXT NOT NULL,
        name                      TEXT,
        industry                  TEXT,
        mode                      TEXT,

        base_window               INTEGER,
        base_high                 REAL,
        base_low                  REAL,
        base_range_pct            REAL,
        breakout_pct              REAL,
        volume_ratio              REAL,
        amount_yi                 REAL,
        ret20_pct                 REAL,
        rel20_pct                 REAL,
        ret60_pct                 REAL,
        ma20                      REAL,
        ma60                      REAL,

        t_open                    REAL,
        t_high                    REAL,
        t_low                     REAL,
        t_close                   REAL,
        t_pct_chg                 REAL,

        entry_zone_low            REAL NOT NULL,
        entry_zone_high           REAL NOT NULL,
        entry_rule                TEXT,

        stop_loss_price           REAL NOT NULL,
        stop_loss_rule            TEXT,
        failure_line_price        REAL,
        failure_line_rule         TEXT,
        exit_rule                 TEXT,

        position_pct              REAL NOT NULL,
        position_calc             TEXT,
        signal_score              REAL,

        UNIQUE(date, code)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_s1_cand_date ON s1_candidates(date);",
    "CREATE INDEX IF NOT EXISTS idx_s1_cand_code ON s1_candidates(code);",
    """
    CREATE TABLE IF NOT EXISTS s1_candidate_rejects (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        date           TEXT NOT NULL,
        code           TEXT NOT NULL,
        name           TEXT,
        stage_failed   TEXT NOT NULL,
        reject_reason  TEXT NOT NULL,
        UNIQUE(date, code)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_s1_rej_date ON s1_candidate_rejects(date);",
    """
    CREATE TABLE IF NOT EXISTS s1_verifications (
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
        FOREIGN KEY(candidate_id) REFERENCES s1_candidates(id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_s1_veri_t1 ON s1_verifications(t1_date);",
    "CREATE INDEX IF NOT EXISTS idx_s1_veri_code ON s1_verifications(code);",
    "CREATE INDEX IF NOT EXISTS idx_s1_veri_mode ON s1_verifications(mode);",
]


def ensure_s1_tables(conn) -> None:
    for stmt in S1_TABLE_DDL:
        conn.execute(stmt)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(s1_verifications)").fetchall()}
    if "t1_open_gap_pct" not in cols:
        conn.execute("ALTER TABLE s1_verifications ADD COLUMN t1_open_gap_pct REAL")


def write_select_run(payload: dict) -> None:
    init_market_db()
    conn = get_market_db()
    try:
        ensure_s1_tables(conn)
        ensure_regime_gate_columns(conn, "s1_select_runs")
        date = payload["date"]
        regime = payload.get("regime_input", {})
        gate = payload.get("regime_gate") or {}
        stats = payload.get("stats") or {}
        conn.execute(
            """
            INSERT OR REPLACE INTO s1_select_runs (
                date, strategy, regime_code, regime_name, regime_score,
                confidence, switched, emergency_switch, position_limit_single_base, mode,
                skipped_reason, regime_gate_allowed, regime_gate_reason,
                universe_size, breakout_pool_size, passed_count, config_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                date,
                payload.get("strategy", "S1"),
                regime.get("code", ""),
                regime.get("regime_name", ""),
                regime.get("score", 0) or 0,
                regime.get("confidence"),
                1 if regime.get("switched") else 0,
                1 if regime.get("emergency_switch") else 0,
                regime.get("position_limit_single_base"),
                payload.get("mode"),
                payload.get("skipped_reason"),
                None if gate.get("allowed") is None else (1 if gate.get("allowed") else 0),
                gate.get("reason"),
                stats.get("universe_size"),
                stats.get("breakout_pool_size"),
                stats.get("passed_count"),
                json.dumps(payload.get("config") or {}, ensure_ascii=False),
            ),
        )

        conn.execute("DELETE FROM s1_verifications WHERE t_date = ?", (date,))
        conn.execute("DELETE FROM s1_candidates WHERE date = ?", (date,))
        _cands = payload.get("candidates") or []
        _names = resolve_names([x.get("code") for x in _cands], as_of=date)
        for candidate in _cands:
            _rn = _names.get(str(candidate.get("code")).split(".")[0].zfill(6))
            if _rn:
                candidate["name"] = _rn  # 回填真名(源表 name 原存代码)
            if is_st(candidate.get("name")):
                continue
            _insert_candidate(conn, date, candidate)

        conn.execute("DELETE FROM s1_candidate_rejects WHERE date = ?", (date,))
        for reject in payload.get("reject_samples") or []:
            _insert_reject(conn, date, reject)

        conn.commit()
        logging.info("DB 写入 S1 select_run: date=%s candidates=%s", date, len(payload.get("candidates") or []))
    finally:
        conn.close()


def _insert_candidate(conn, date: str, candidate: dict) -> None:
    signal = candidate["signal"]
    bar = candidate["t_bar"]
    entry = candidate["entry"]
    stop_loss = candidate["stop_loss"]
    failure_line = candidate["failure_line"]
    conn.execute(
        """
        INSERT INTO s1_candidates (
            date, code, name, industry, mode,
            base_window, base_high, base_low, base_range_pct, breakout_pct,
            volume_ratio, amount_yi, ret20_pct, rel20_pct, ret60_pct, ma20, ma60,
            t_open, t_high, t_low, t_close, t_pct_chg,
            entry_zone_low, entry_zone_high, entry_rule,
            stop_loss_price, stop_loss_rule, failure_line_price, failure_line_rule,
            exit_rule, position_pct, position_calc, signal_score
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            date,
            candidate["code"], candidate.get("name"), candidate.get("industry"), candidate.get("mode"),
            signal.get("base_window"), signal.get("base_high"), signal.get("base_low"),
            signal.get("base_range_pct"), signal.get("breakout_pct"),
            signal.get("volume_ratio"), signal.get("amount_yi"),
            signal.get("ret20_pct"), signal.get("rel20_pct"), signal.get("ret60_pct"),
            signal.get("ma20"), signal.get("ma60"),
            bar.get("open"), bar.get("high"), bar.get("low"), bar.get("close"), bar.get("pct_chg"),
            entry["zone_low"], entry["zone_high"], entry.get("rule"),
            stop_loss["price"], stop_loss.get("rule"),
            failure_line["price"], failure_line.get("rule"),
            candidate.get("exit_rule"), candidate["position_pct"], candidate.get("position_calc"),
            signal.get("score"),
        ),
    )


def _insert_reject(conn, date: str, reject: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO s1_candidate_rejects (date, code, name, stage_failed, reject_reason)
        VALUES (?, ?, ?, ?, ?)
        """,
        (date, reject["code"], reject.get("name"), reject["stage_failed"], reject["reject_reason"]),
    )


def write_verification(t1_date: str, t_date: str, results: list, mode: str) -> None:
    init_market_db()
    conn = get_market_db()
    try:
        ensure_s1_tables(conn)
        conn.execute("DELETE FROM s1_verifications WHERE t1_date = ? AND mode = ?", (t1_date, mode))
        for item in results:
            candidate = item.get("candidate", {})
            verification = item.get("verification", {})
            code = candidate.get("code")
            row = conn.execute(
                "SELECT id FROM s1_candidates WHERE date = ? AND code = ?",
                (t_date, code),
            ).fetchone()
            candidate_id = row["id"] if row else 0
            conn.execute(
                """
                INSERT INTO s1_verifications (
                    t_date, t1_date, candidate_id, code, mode, status,
                    entry_status, entry_price, exit_price, exit_reason, pnl_pct,
                    t1_open, t1_high, t1_low, t1_close, t1_open_gap_pct,
                    live_open_price, live_current, note, hold_days, exit_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t_date, t1_date, candidate_id, code, mode, verification.get("status", "unknown"),
                    verification.get("entry_status"), verification.get("entry_price"),
                    verification.get("exit_price"), verification.get("exit_reason"),
                    verification.get("pnl_pct"),
                    verification.get("t1_open"), verification.get("t1_high"),
                    verification.get("t1_low"), verification.get("t1_close"),
                    verification.get("t1_open_gap_pct"),
                    verification.get("open_price"), verification.get("current"),
                    verification.get("note"), verification.get("hold_days"), verification.get("exit_date"),
                ),
            )
        conn.commit()
        logging.info("DB 写入 S1 verifications: t1_date=%s rows=%s", t1_date, len(results))
    finally:
        conn.close()


def read_select_run(date: str) -> Optional[dict]:
    init_market_db()
    conn = get_market_db()
    try:
        ensure_s1_tables(conn)
        run = conn.execute("SELECT * FROM s1_select_runs WHERE date = ?", (date,)).fetchone()
        if not run:
            return None
        cand_rows = conn.execute(
            "SELECT * FROM s1_candidates WHERE date = ? ORDER BY signal_score DESC",
            (date,),
        ).fetchall()
        reject_rows = conn.execute(
            "SELECT * FROM s1_candidate_rejects WHERE date = ?",
            (date,),
        ).fetchall()
    finally:
        conn.close()

    gate_allowed_raw = run["regime_gate_allowed"] if "regime_gate_allowed" in run.keys() else None
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
        "regime_gate": {
            "allowed": None if gate_allowed_raw is None else bool(gate_allowed_raw),
            "reason": run["regime_gate_reason"] if "regime_gate_reason" in run.keys() else None,
            "playbook_mode": run["mode"],
        },
        "candidates": [_row_to_candidate(row) for row in cand_rows],
        "reject_samples": [
            {
                "code": row["code"],
                "name": row["name"],
                "stage_failed": row["stage_failed"],
                "reject_reason": row["reject_reason"],
            }
            for row in reject_rows
        ],
        "skipped_reason": run["skipped_reason"],
        "stats": None if run["skipped_reason"] else {
            "universe_size": run["universe_size"],
            "breakout_pool_size": run["breakout_pool_size"],
            "passed_count": run["passed_count"],
        },
    }


def _row_to_candidate(row) -> dict:
    return {
        "code": row["code"],
        "name": row["name"],
        "industry": row["industry"],
        "mode": row["mode"],
        "signal": {
            "base_window": row["base_window"],
            "base_high": row["base_high"],
            "base_low": row["base_low"],
            "base_range_pct": row["base_range_pct"],
            "breakout_pct": row["breakout_pct"],
            "volume_ratio": row["volume_ratio"],
            "amount_yi": row["amount_yi"],
            "ret20_pct": row["ret20_pct"],
            "rel20_pct": row["rel20_pct"],
            "ret60_pct": row["ret60_pct"],
            "ma20": row["ma20"],
            "ma60": row["ma60"],
            "score": row["signal_score"],
        },
        "t_bar": {
            "open": row["t_open"],
            "high": row["t_high"],
            "low": row["t_low"],
            "close": row["t_close"],
            "pct_chg": row["t_pct_chg"],
        },
        "entry": {
            "zone_low": row["entry_zone_low"],
            "zone_high": row["entry_zone_high"],
            "rule": row["entry_rule"],
        },
        "stop_loss": {
            "price": row["stop_loss_price"],
            "rule": row["stop_loss_rule"],
        },
        "failure_line": {
            "price": row["failure_line_price"],
            "rule": row["failure_line_rule"],
        },
        "exit_rule": row["exit_rule"],
        "position_pct": row["position_pct"],
        "position_calc": row["position_calc"],
    }


def read_verification(t1_date: str, mode: str | None = None) -> Optional[dict]:
    init_market_db()
    conn = get_market_db()
    try:
        ensure_s1_tables(conn)
        if mode:
            rows = conn.execute(
                "SELECT * FROM s1_verifications WHERE t1_date = ? AND mode = ?",
                (t1_date, mode),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM s1_verifications WHERE t1_date = ?",
                (t1_date,),
            ).fetchall()
        if not rows:
            return None
        t_date = rows[0]["t_date"]
        cand_rows = conn.execute("SELECT * FROM s1_candidates WHERE date = ?", (t_date,)).fetchall()
        candidates_by_id = {row["id"]: _row_to_candidate(row) for row in cand_rows}
    finally:
        conn.close()

    return {
        "t1_date": t1_date,
        "t_date": t_date,
        "mode": rows[0]["mode"],
        "results": [
            {
                "candidate": candidates_by_id.get(row["candidate_id"], {"code": row["code"], "name": "?"}),
                "verification": {
                    "status": row["status"],
                    "entry_status": row["entry_status"],
                    "entry_price": row["entry_price"],
                    "exit_price": row["exit_price"],
                    "exit_reason": row["exit_reason"],
                    "pnl_pct": row["pnl_pct"],
                    "t1_open": row["t1_open"],
                    "t1_high": row["t1_high"],
                    "t1_low": row["t1_low"],
                    "t1_close": row["t1_close"],
                    "t1_open_gap_pct": row["t1_open_gap_pct"] if "t1_open_gap_pct" in row.keys() else None,
                    "open_price": row["live_open_price"],
                    "current": row["live_current"],
                    "note": row["note"],
                    "hold_days": row["hold_days"],
                    "exit_date": row["exit_date"],
                },
            }
            for row in rows
        ],
    }
