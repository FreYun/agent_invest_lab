"""S3 DB persistence helpers."""

from __future__ import annotations

import json
import logging
from typing import Optional

from strategy_common import ensure_regime_gate_columns, get_market_db, init_market_db, is_st, resolve_names


def write_select_run(payload: dict) -> None:
    init_market_db()
    conn = get_market_db()
    try:
        ensure_regime_gate_columns(conn, "s3_select_runs")
        date = payload["date"]
        regime = payload.get("regime_input", {})
        gate = payload.get("regime_gate") or {}
        stats = payload.get("stats") or {}
        conn.execute(
            """
            INSERT OR REPLACE INTO s3_select_runs (
                date, strategy, regime_code, regime_name, regime_score,
                confidence, switched, emergency_switch, position_limit_single_base, mode,
                skipped_reason, regime_gate_allowed, regime_gate_reason,
                universe_size, first_board_pool_size, passed_count,
                hot_industries_json, config_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                date,
                payload.get("strategy", "S3"),
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
                stats.get("first_board_pool_size"),
                stats.get("passed_count"),
                json.dumps(stats.get("hot_industries", []), ensure_ascii=False) if stats else None,
                json.dumps(payload.get("config") or {}, ensure_ascii=False),
            ),
        )

        conn.execute("DELETE FROM s3_verifications WHERE t_date = ?", (date,))
        conn.execute("DELETE FROM s3_candidates WHERE date = ?", (date,))
        _cands = payload.get("candidates") or []
        _names = resolve_names([x.get("code") for x in _cands], as_of=date)
        for c in _cands:
            _rn = _names.get(str(c.get("code")).split(".")[0].zfill(6))
            if _rn:
                c["name"] = _rn  # 回填真名(源表 name 原存代码)
            if is_st(c.get("name")):
                continue
            _insert_candidate(conn, date, c)

        conn.execute("DELETE FROM s3_candidate_rejects WHERE date = ?", (date,))
        for r in payload.get("reject_samples") or []:
            _insert_reject(conn, date, r)

        conn.commit()
        logging.info("DB 写入 S3 select_run: date=%s candidates=%s", date, len(payload.get("candidates") or []))
    finally:
        conn.close()


def _insert_candidate(conn, date: str, c: dict) -> None:
    s = c["signal"]
    b = c["t_bar"]
    e = c["entry"]
    sl = c["stop_loss"]
    conn.execute(
        """
        INSERT INTO s3_candidates (
            date, code, name, industry,
            sector_rank, sector_limit_count, streak, first_seal_time, last_seal_time,
            blast_count, seal_amount, turnover_rate, amount,
            t_open, t_high, t_low, t_close, t_pct_chg,
            entry_zone_low, entry_zone_high, entry_rule,
            stop_loss_price, stop_loss_rule, exit_rule,
            position_pct, position_calc, signal_score
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            date,
            c["code"], c.get("name"), c.get("industry"),
            s.get("sector_rank"), s.get("sector_limit_count"), s.get("streak"),
            s.get("first_seal_time"), s.get("last_seal_time"),
            s.get("blast_count"), s.get("seal_amount"), s.get("turnover_rate"), s.get("amount"),
            b.get("open"), b.get("high"), b.get("low"), b.get("close"), b.get("pct_chg"),
            e["zone_low"], e["zone_high"], e.get("rule"),
            sl["price"], sl.get("rule"), c.get("exit_rule"),
            c["position_pct"], c.get("position_calc"), s.get("score"),
        ),
    )


def _insert_reject(conn, date: str, r: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO s3_candidate_rejects (date, code, name, stage_failed, reject_reason)
        VALUES (?, ?, ?, ?, ?)
        """,
        (date, r["code"], r.get("name"), r["stage_failed"], r["reject_reason"]),
    )


def write_verification(t1_date: str, t_date: str, results: list, mode: str) -> None:
    init_market_db()
    conn = get_market_db()
    try:
        conn.execute("DELETE FROM s3_verifications WHERE t1_date = ? AND mode = ?", (t1_date, mode))
        for item in results:
            cand = item.get("candidate", {})
            v = item.get("verification", {})
            code = cand.get("code")
            row = conn.execute(
                "SELECT id FROM s3_candidates WHERE date = ? AND code = ?",
                (t_date, code),
            ).fetchone()
            candidate_id = row["id"] if row else 0
            conn.execute(
                """
                INSERT INTO s3_verifications (
                    t_date, t1_date, candidate_id, code, mode, status,
                    entry_status, entry_price, exit_price, exit_reason, pnl_pct,
                    t1_open, t1_high, t1_low, t1_close,
                    live_open_price, live_current, note, hold_days, exit_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t_date, t1_date, candidate_id, code, mode, v.get("status", "unknown"),
                    v.get("entry_status"), v.get("entry_price"), v.get("exit_price"),
                    v.get("exit_reason"), v.get("pnl_pct"),
                    v.get("t1_open"), v.get("t1_high"), v.get("t1_low"), v.get("t1_close"),
                    v.get("open_price"), v.get("current"), v.get("note"),
                    v.get("hold_days"), v.get("exit_date"),
                ),
            )
        conn.commit()
        logging.info("DB 写入 S3 verifications: t1_date=%s rows=%s", t1_date, len(results))
    finally:
        conn.close()


def read_select_run(date: str) -> Optional[dict]:
    init_market_db()
    conn = get_market_db()
    try:
        run = conn.execute("SELECT * FROM s3_select_runs WHERE date = ?", (date,)).fetchone()
        if not run:
            return None
        cand_rows = conn.execute(
            "SELECT * FROM s3_candidates WHERE date = ? ORDER BY signal_score DESC",
            (date,),
        ).fetchall()
        reject_rows = conn.execute(
            "SELECT * FROM s3_candidate_rejects WHERE date = ?",
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
        "candidates": [_row_to_candidate(r) for r in cand_rows],
        "reject_samples": [
            {
                "code": r["code"], "name": r["name"],
                "stage_failed": r["stage_failed"], "reject_reason": r["reject_reason"],
            }
            for r in reject_rows
        ],
        "skipped_reason": run["skipped_reason"],
        "stats": None if run["skipped_reason"] else {
            "universe_size": run["universe_size"],
            "first_board_pool_size": run["first_board_pool_size"],
            "passed_count": run["passed_count"],
            "hot_industries": json.loads(run["hot_industries_json"] or "[]"),
        },
    }


def _row_to_candidate(r) -> dict:
    return {
        "code": r["code"],
        "name": r["name"],
        "industry": r["industry"],
        "signal": {
            "sector_rank": r["sector_rank"],
            "sector_limit_count": r["sector_limit_count"],
            "streak": r["streak"],
            "first_seal_time": r["first_seal_time"],
            "last_seal_time": r["last_seal_time"],
            "blast_count": r["blast_count"],
            "seal_amount": r["seal_amount"],
            "turnover_rate": r["turnover_rate"],
            "amount": r["amount"],
            "score": r["signal_score"],
        },
        "t_bar": {
            "open": r["t_open"], "high": r["t_high"], "low": r["t_low"],
            "close": r["t_close"], "pct_chg": r["t_pct_chg"],
        },
        "entry": {"zone_low": r["entry_zone_low"], "zone_high": r["entry_zone_high"], "rule": r["entry_rule"]},
        "stop_loss": {"price": r["stop_loss_price"], "rule": r["stop_loss_rule"]},
        "exit_rule": r["exit_rule"],
        "position_pct": r["position_pct"],
        "position_calc": r["position_calc"],
    }


def read_verification(t1_date: str, mode: str | None = None) -> Optional[dict]:
    init_market_db()
    conn = get_market_db()
    try:
        if mode:
            rows = conn.execute(
                "SELECT * FROM s3_verifications WHERE t1_date = ? AND mode = ?",
                (t1_date, mode),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM s3_verifications WHERE t1_date = ?",
                (t1_date,),
            ).fetchall()
        if not rows:
            return None
        t_date = rows[0]["t_date"]
        cand_rows = conn.execute("SELECT * FROM s3_candidates WHERE date = ?", (t_date,)).fetchall()
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
