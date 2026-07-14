"""S5 DB 高层写/读 helper — select.py / verify.py / render.py 都用这里。

写函数:
  write_select_run(payload)        — payload 是 select.py 的 run_select 返回 dict
  write_verification(payload, mode) — payload 是 verify.py 写 json 的 dict {t1_date, t_date, mode, results}

读函数:
  read_select_run(date) → dict       — 重建出和 candidates_*.json 等价的 payload
  read_verification(t1_date) → dict — 重建出和 verification_*.json 等价的 payload

设计:
  - 写: INSERT OR REPLACE, 重跑同一 date 会覆盖
  - 读: 完整复原 file json 字段格式, 让 render 可以直接调 output_writer.render_md
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Optional

# 拉 s5/_lib/db
_LIB_DIR = "/home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/s5/_lib"
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
import db  # noqa: E402

_ROOT_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT_SCRIPTS not in sys.path:
    sys.path.insert(0, _ROOT_SCRIPTS)
from strategy_common import ensure_regime_gate_columns, is_st, resolve_names  # noqa: E402


# --------------------------------------------------------------------------- #
# 写: select_run + candidates + rejects
# --------------------------------------------------------------------------- #


def write_select_run(payload: dict):
    """把 select.py run_select 返回的 payload 写入 s5.db。

    会清掉该 date 的 candidates 和 rejects 后重新插入 (幂等)。
    """
    db.init_market_db()
    conn = db.get_market_db()
    try:
        ensure_regime_gate_columns(conn, "s5_select_runs")
        date = payload["date"]
        regime = payload.get("regime_input", {})
        gate = payload.get("regime_gate") or {}
        stats = payload.get("stats") or {}

        conn.execute(
            """
            INSERT OR REPLACE INTO s5_select_runs (
                date, strategy, regime_code, regime_name, regime_score,
                confidence, switched, emergency_switch, position_limit_single_base,
                skipped_reason, regime_gate_allowed, regime_gate_reason,
                universe_size, dragon_pool_size, passed_count,
                hot_industries_json, config_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                date,
                payload.get("strategy", "S5"),
                regime.get("code", ""),
                regime.get("regime_name", ""),
                regime.get("score", 0) or 0,
                regime.get("confidence"),
                1 if regime.get("switched") else 0,
                1 if regime.get("emergency_switch") else 0,
                regime.get("position_limit_single_base"),
                payload.get("skipped_reason"),
                None if gate.get("allowed") is None else (1 if gate.get("allowed") else 0),
                gate.get("reason"),
                stats.get("universe_size"),
                stats.get("dragon_pool_size"),
                stats.get("passed_count"),
                json.dumps(stats.get("hot_industries", []), ensure_ascii=False) if stats else None,
                None,  # config_json: v1 不快照
            ),
        )

        conn.execute("DELETE FROM s5_candidates WHERE date = ?", (date,))
        _cands = payload.get("candidates") or []
        _names = resolve_names([x.get("code") for x in _cands], as_of=date)
        for c in _cands:
            _rn = _names.get(str(c.get("code")).split(".")[0].zfill(6))
            if _rn:
                c["name"] = _rn  # 回填真名(源表 name 原存代码)
            if is_st(c.get("name")):
                continue
            _insert_candidate(conn, date, c)

        conn.execute("DELETE FROM s5_candidate_rejects WHERE date = ?", (date,))
        for r in payload.get("reject_samples") or []:
            _insert_reject(conn, date, r)

        conn.commit()
        logging.info(f"DB 写入 select_run: date={date}, candidates={len(payload.get('candidates') or [])}")
    finally:
        conn.close()


def _insert_candidate(conn, date: str, c: dict):
    peak = c["dragon_peak"]
    cd = c["cooldown"]
    rb = c["rebound"]
    entry = c["entry"]
    sl = c["stop_loss"]
    t1 = c.get("target_1", {})
    t2 = c.get("target_2", {})
    conn.execute(
        """
        INSERT INTO s5_candidates (
            date, code, name, industry,
            dragon_peak_date, dragon_peak_close, dragon_peak_max_streak,
            cooldown_days, cooldown_drop_pct, cooldown_t1_close,
            rebound_t_pct, rebound_t_close, rebound_t_low, rebound_t_high, rebound_t1_high,
            entry_zone_low, entry_zone_high, entry_rule,
            stop_loss_price, stop_loss_rule,
            target_1_price, target_2_price,
            position_pct, position_calc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            date,
            c["code"], c.get("name"), c.get("industry"),
            peak["date"], float(peak["close"]), int(peak["max_streak"]),
            int(cd["days"]), float(cd["drop_pct"]), cd.get("t1_close"),
            float(rb["t_pct"]), float(rb["t_close"]), rb.get("t_low"), rb.get("t_high"), rb.get("t1_high"),
            float(entry["zone_low"]), float(entry["zone_high"]), entry.get("rule"),
            float(sl["price"]), sl.get("rule"),
            t1.get("price"), t2.get("price"),
            float(c["position_pct"]), c.get("position_calc"),
        ),
    )


def _insert_reject(conn, date: str, r: dict):
    conn.execute(
        """
        INSERT OR REPLACE INTO s5_candidate_rejects (date, code, name, stage_failed, reject_reason)
        VALUES (?, ?, ?, ?, ?)
        """,
        (date, r["code"], r.get("name"), r["stage_failed"], r["reject_reason"]),
    )


# --------------------------------------------------------------------------- #
# 写: verifications
# --------------------------------------------------------------------------- #


def write_verification(t1_date: str, t_date: str, results: list, mode: str):
    """写 verifications 表。会清掉该 t1_date 的旧记录后重新插。"""
    db.init_market_db()
    conn = db.get_market_db()
    try:
        conn.execute("DELETE FROM s5_verifications WHERE t1_date = ?", (t1_date,))
        inserted = 0
        for item in results:
            cand = item.get("candidate", {})
            v = item.get("verification", {})
            code = cand.get("code")
            if not code:
                continue
            row = conn.execute(
                "SELECT id FROM s5_candidates WHERE date = ? AND code = ?",
                (t_date, code),
            ).fetchone()
            candidate_id = row["id"] if row else 0
            conn.execute(
                """
                INSERT INTO s5_verifications (
                    t_date, t1_date, candidate_id, code, mode,
                    status, entry_status, entry_price, exit_price, exit_reason, pnl_pct,
                    t1_open, t1_high, t1_low, t1_close,
                    live_open_price, live_current, note,
                    hold_days, exit_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t_date, t1_date, candidate_id, code, mode,
                    v.get("status", "unknown"),
                    v.get("entry_status"),
                    v.get("entry_price"),
                    v.get("exit_price"),
                    v.get("exit_reason"),
                    v.get("pnl_pct"),
                    v.get("t1_open"), v.get("t1_high"), v.get("t1_low"), v.get("t1_close"),
                    v.get("open_price"),
                    v.get("current"),
                    v.get("note"),
                    v.get("hold_days"),
                    v.get("exit_date"),
                ),
            )
            inserted += 1
        conn.commit()
        logging.info(f"DB 写入 verifications: t1_date={t1_date}, mode={mode}, rows={inserted}")
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 读: 复原 select_run payload (供 render.py 用)
# --------------------------------------------------------------------------- #


def read_select_run(date: str) -> Optional[dict]:
    """从 s5.db 读出 select_run 完整 payload, 返回和 candidates_*.json 等价的 dict。

    返回 None 表示该日期没有 select_run 记录。
    """
    db.init_market_db()
    conn = db.get_market_db()
    try:
        run = conn.execute(
            "SELECT * FROM s5_select_runs WHERE date = ?", (date,)
        ).fetchone()
        if not run:
            return None

        cand_rows = conn.execute(
            "SELECT * FROM s5_candidates WHERE date = ? ORDER BY rebound_t_pct DESC", (date,)
        ).fetchall()
        reject_rows = conn.execute(
            "SELECT * FROM s5_candidate_rejects WHERE date = ?", (date,)
        ).fetchall()
    finally:
        conn.close()

    candidates = [_row_to_candidate(r) for r in cand_rows]
    reject_samples = [
        {
            "code": r["code"],
            "name": r["name"],
            "stage_failed": r["stage_failed"],
            "reject_reason": r["reject_reason"],
        }
        for r in reject_rows
    ]

    gate_allowed_raw = run["regime_gate_allowed"] if "regime_gate_allowed" in run.keys() else None
    payload = {
        "date": run["date"],
        "strategy": run["strategy"],
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
            "playbook_mode": None,
        },
        "candidates": candidates,
        "reject_samples": reject_samples,
        "skipped_reason": run["skipped_reason"],
        "stats": None
        if run["skipped_reason"]
        else {
            "universe_size": run["universe_size"],
            "dragon_pool_size": run["dragon_pool_size"],
            "passed_count": run["passed_count"],
            "hot_industries": json.loads(run["hot_industries_json"] or "[]"),
        },
    }
    return payload


def _row_to_candidate(r) -> dict:
    return {
        "code": r["code"],
        "name": r["name"],
        "industry": r["industry"],
        "dragon_peak": {
            "date": r["dragon_peak_date"],
            "close": r["dragon_peak_close"],
            "max_streak": r["dragon_peak_max_streak"],
        },
        "cooldown": {
            "days": r["cooldown_days"],
            "drop_pct": r["cooldown_drop_pct"],
            "t1_close": r["cooldown_t1_close"],
        },
        "rebound": {
            "t_pct": r["rebound_t_pct"],
            "t_close": r["rebound_t_close"],
            "t_low": r["rebound_t_low"],
            "t_high": r["rebound_t_high"],
            "t1_high": r["rebound_t1_high"],
        },
        "entry": {
            "zone_low": r["entry_zone_low"],
            "zone_high": r["entry_zone_high"],
            "rule": r["entry_rule"],
        },
        "stop_loss": {
            "price": r["stop_loss_price"],
            "rule": r["stop_loss_rule"],
        },
        "target_1": {"price": r["target_1_price"], "pct": None},
        "target_2": {"price": r["target_2_price"], "pct": None},
        "exit_rule": "涨停跟随: 封停持有, 炸板/破止损卖出",
        "position_pct": r["position_pct"],
        "position_calc": r["position_calc"],
    }


# --------------------------------------------------------------------------- #
# 读: 复原 verification payload
# --------------------------------------------------------------------------- #


def read_verification(t1_date: str) -> Optional[dict]:
    """读 verifications 表, 返回和 verification_*.json 等价的 dict。"""
    db.init_market_db()
    conn = db.get_market_db()
    try:
        rows = conn.execute(
            """
            SELECT v.*, c.id AS cand_id_check
            FROM s5_verifications v
            LEFT JOIN s5_candidates c ON c.id = v.candidate_id
            WHERE v.t1_date = ?
            """,
            (t1_date,),
        ).fetchall()
        if not rows:
            return None

        t_date = rows[0]["t_date"]
        mode = rows[0]["mode"]

        # 拉对应 candidates
        cand_rows = conn.execute(
            "SELECT * FROM s5_candidates WHERE date = ?", (t_date,)
        ).fetchall()
        cand_by_id = {r["id"]: _row_to_candidate(r) for r in cand_rows}
    finally:
        conn.close()

    results = []
    for r in rows:
        cand = cand_by_id.get(r["candidate_id"], {"code": r["code"], "name": "?"})
        verification = {
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
        }
        results.append({"candidate": cand, "verification": verification})

    return {
        "t1_date": t1_date,
        "t_date": t_date,
        "mode": mode,
        "results": results,
    }
