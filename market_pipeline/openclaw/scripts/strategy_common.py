"""Shared helpers for strategy scripts backed by market.db.

This module deliberately uses only local market.db data for select/backtest paths.
Live intraday checks can still use strategy-specific realtime adapters.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta
from typing import Optional

DB_PATH = os.environ.get("MARKET_DB_PATH") or os.environ.get("SCOUT_DB_PATH") or "/home/rooot/agent_invest_lab/data/market.db"
S5_LIB_DIR = "/home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/s5/_lib"
MARKET_REGIME_SCRIPTS = "/home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/market-regime/scripts"


if S5_LIB_DIR not in sys.path:
    sys.path.insert(0, S5_LIB_DIR)
import db as market_db  # noqa: E402

if MARKET_REGIME_SCRIPTS not in sys.path:
    sys.path.insert(0, MARKET_REGIME_SCRIPTS)
from regime_rules import lookup_playbook  # noqa: E402


class RegimeNotFound(Exception):
    pass


def init_market_db() -> None:
    market_db.init_market_db()


def get_market_db() -> sqlite3.Connection:
    return market_db.get_market_db()


def load_regime_from_db(trade_date: str, rules_version: str = "v2") -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT * FROM regime_classify_daily
            WHERE trade_date = ? AND rules_version = ?
            """,
            (trade_date, rules_version),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise RegimeNotFound(
            f"regime_classify_daily 无 {trade_date} 的 {rules_version} 记录. "
            f"先跑 pipeline.sh 或 replay.py 生成."
        )

    return {
        "date": row["trade_date"],
        "regime": row["regime_name"],
        "regime_code": row["regime_code"],
        "score": {
            "total": row["total_score"],
            "breakdown": {
                "ma_position": row["score_ma_position"],
                "advance_decline": row["score_advance_decline"],
                "sentiment_delta": row["score_sentiment_delta"],
                "sentiment_index": row["score_sentiment_index"],
                "streak_height": row["score_streak_height"],
                "volume_trend": row["score_volume_trend"],
            },
        },
        "confidence": row["confidence"],
        "switched": bool(row["switched"]),
        "emergency_switch": bool(row["emergency_switch"]),
        "playbook": lookup_playbook(row["regime_code"]),
        "source": {"kind": "db", "rules_version": rules_version},
    }


def regime_input_summary(regime_data: dict) -> dict:
    return {
        "code": regime_data.get("regime_code"),
        "regime_name": regime_data.get("regime"),
        "score": regime_data.get("score", {}).get("total"),
        "confidence": regime_data.get("confidence"),
        "switched": regime_data.get("switched"),
        "emergency_switch": regime_data.get("emergency_switch"),
        "position_limit_single_base": regime_data.get("playbook", {}).get("position_limit", {}).get("single"),
    }


def strategy_playbook_entry(regime_data: dict, strategy_id: str) -> Optional[dict]:
    recommended = regime_data.get("playbook", {}).get("recommended", [])
    for item in recommended:
        if item.get("id") == strategy_id:
            return item
    return None


def is_strategy_allowed(regime_data: dict, strategy_id: str) -> tuple[bool, str | None, dict | None]:
    entry = strategy_playbook_entry(regime_data, strategy_id)
    if entry is None:
        rec_ids = [item.get("id") for item in regime_data.get("playbook", {}).get("recommended", [])]
        regime_name = regime_data.get("regime", regime_data.get("regime_code", "UNKNOWN"))
        return False, f"regime={regime_name}, {strategy_id} not in playbook.recommended ({rec_ids})", None
    return True, None, entry


def ensure_regime_gate_columns(conn, table: str) -> None:
    """Idempotently add regime_gate_allowed/regime_gate_reason to a *_select_runs table.

    Skipped silently when columns already exist. Strategy db_writers call this
    before INSERT so legacy tables get migrated on the next run.
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if "regime_gate_allowed" not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN regime_gate_allowed INTEGER")
    if "regime_gate_reason" not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN regime_gate_reason TEXT")


def build_regime_gate(
    allowed: bool,
    reason: str | None,
    playbook_mode: str | None,
) -> dict:
    """Advisory metadata describing whether the current regime favors this strategy.

    Strategies emit candidates regardless of this gate; the agent layer decides
    whether to act. `allowed` mirrors the legacy is_strategy_allowed verdict so
    the agent / dashboard can still see "regime says no" without losing the
    candidate list.
    """
    return {
        "allowed": bool(allowed),
        "reason": reason,
        "playbook_mode": playbook_mode,
    }


def calculate_position_pct(
    regime_data: dict,
    extra_multiplier: float = 1.0,
    extra_label: str | None = None,
) -> tuple[float, str]:
    playbook = regime_data.get("playbook", {})
    pos_limit = playbook.get("position_limit", {})
    base = float(pos_limit.get("single", 0.10))
    final = base
    parts = [f"{base:.2f} (base)"]

    if extra_multiplier != 1.0:
        final *= extra_multiplier
        label = extra_label or "strategy_multiplier"
        parts.append(f"× {extra_multiplier:.2f} ({label})")

    confidence = regime_data.get("confidence", "high")
    if confidence == "low":
        final *= 0.5
        parts.append("× 0.5 (confidence=low)")
    elif confidence == "medium":
        final *= 0.8
        parts.append("× 0.8 (confidence=medium)")

    if regime_data.get("switched"):
        final *= 0.8
        parts.append("× 0.8 (switched)")

    if regime_data.get("emergency_switch"):
        final *= 0.7
        parts.append("× 0.7 (emergency_switch)")

    return round(final, 4), " ".join(parts) + f" = {final:.4f}"


def is_st(name) -> bool:
    """统一 ST/退市风险股判定: 名称含 "ST"(覆盖 ST/*ST/SST 等) 或含 "退"(退市整理期) 即剔除.

    各战法(S1~S9)选股写库前据此过滤, 保证看板/战绩口径一致. 名称为空时返回 False(不误杀).
    """
    s = str(name or "")
    return "ST" in s.upper() or "退" in s


def parse_seal_time(value: str | None) -> int | None:
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if not digits:
        return None
    if len(digits) <= 4:
        digits = digits.zfill(4) + "00"
    elif len(digits) == 5:
        digits = "0" + digits
    elif len(digits) > 6:
        digits = digits[:6]
    return int(digits)


def yyyymmdd_to_iso(value: str) -> str:
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"


def iso_to_yyyymmdd(value: str) -> str:
    return value.replace("-", "")


def get_trading_days(start: str, end: str) -> list[str]:
    start_raw = iso_to_yyyymmdd(start)
    end_raw = iso_to_yyyymmdd(end)
    conn = get_market_db()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT trade_date FROM daily
            WHERE trade_date >= ? AND trade_date <= ?
            ORDER BY trade_date
            """,
            (start_raw, end_raw),
        ).fetchall()
    finally:
        conn.close()
    return [yyyymmdd_to_iso(r["trade_date"]) for r in rows]


def shift_trading_days(date_str: str, n_days: int) -> str:
    raw = iso_to_yyyymmdd(date_str)
    conn = get_market_db()
    try:
        if n_days >= 0:
            rows = conn.execute(
                """
                SELECT DISTINCT trade_date FROM daily
                WHERE trade_date >= ?
                ORDER BY trade_date ASC
                LIMIT ?
            """,
                (raw, n_days + 1),
            ).fetchall()
            if rows:
                first_is_input = rows[0]["trade_date"] == raw
                offset = n_days if first_is_input else max(n_days - 1, 0)
                if len(rows) > offset:
                    return yyyymmdd_to_iso(rows[offset]["trade_date"])
        else:
            rows = conn.execute(
                """
                SELECT DISTINCT trade_date FROM daily
                WHERE trade_date <= ?
                ORDER BY trade_date DESC
                LIMIT ?
            """,
                (raw, abs(n_days) + 1),
            ).fetchall()
            if rows:
                first_is_input = rows[0]["trade_date"] == raw
                offset = abs(n_days) if first_is_input else abs(n_days) - 1
                if offset >= 0 and len(rows) > offset:
                    return yyyymmdd_to_iso(rows[offset]["trade_date"])
    finally:
        conn.close()

    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d + timedelta(days=int(n_days * 1.4))).strftime("%Y-%m-%d")


def next_trading_day(date_str: str) -> str | None:
    raw = iso_to_yyyymmdd(date_str)
    conn = get_market_db()
    try:
        row = conn.execute(
            """
            SELECT DISTINCT trade_date FROM daily
            WHERE trade_date > ?
            ORDER BY trade_date ASC
            LIMIT 1
            """,
            (raw,),
        ).fetchone()
    finally:
        conn.close()
    return yyyymmdd_to_iso(row["trade_date"]) if row else None


def read_hot_industries(date_str: str) -> list[dict]:
    conn = get_market_db()
    try:
        rows = conn.execute(
            """
            SELECT rank, industry, limit_count
            FROM hot_industries_daily
            WHERE date = ?
            ORDER BY rank
            """,
            (date_str,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"rank": r["rank"], "name": r["industry"], "limit_count": r["limit_count"]}
        for r in rows
    ]


def read_limit_up_pool(date_str: str) -> list[dict]:
    conn = get_market_db()
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM limit_up_pool
            WHERE date = ?
            ORDER BY streak DESC, code
            """,
            (date_str,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def read_limit_ups_derived(date_str: str, lookback_days: int = 15) -> list[dict]:
    """Derive T-day limit-up rows from daily + stk_limit using only data <= T.

    limit_up_pool is richer (industry/seal time), but long-cycle backtests should not
    require that cache. This function computes limit-up streak from the local daily
    table, then merges optional same-day limit_up_pool fields when available.
    """
    start = shift_trading_days(date_str, -lookback_days)
    start_raw = iso_to_yyyymmdd(start)
    end_raw = iso_to_yyyymmdd(date_str)
    conn = get_market_db()
    try:
        rows = conn.execute(
            """
            SELECT d.trade_date, substr(d.ts_code, 1, 6) AS code,
                   d.open, d.high, d.low, d.close, d.pct_chg, d.vol, d.amount,
                   l.up_limit
            FROM daily d
            INNER JOIN stk_limit l ON l.trade_date = d.trade_date AND l.ts_code = d.ts_code
            WHERE d.trade_date >= ? AND d.trade_date <= ?
              AND l.up_limit IS NOT NULL
              AND d.close >= l.up_limit - 0.01
            ORDER BY d.trade_date, code
            """,
            (start_raw, end_raw),
        ).fetchall()
        pool_rows = conn.execute(
            "SELECT * FROM limit_up_pool WHERE date = ?",
            (date_str,),
        ).fetchall()
    finally:
        conn.close()

    by_date: dict[str, set[str]] = {}
    row_by_key: dict[tuple[str, str], dict] = {}
    for r in rows:
        iso = yyyymmdd_to_iso(r["trade_date"])
        code = r["code"]
        by_date.setdefault(iso, set()).add(code)
        row_by_key[(iso, code)] = {
            "date": iso,
            "code": code,
            "name": code,
            "industry": "未知",
            "pct_chg": float(r["pct_chg"] or 0),
            "streak": 1,
            "close": float(r["close"] or 0),
            "amount": float(r["amount"] or 0) * 1000,
            "market_cap": None,
            "turnover_rate": None,
            "seal_amount": None,
            "first_seal_time": None,
            "last_seal_time": None,
            "blast_count": None,
            "source": "daily_stk_limit",
        }

    prev: dict[str, int] = {}
    for iso in sorted(by_date):
        today_codes = by_date[iso]
        current = {code: prev.get(code, 0) + 1 for code in today_codes}
        for code, streak in current.items():
            row_by_key[(iso, code)]["streak"] = streak
        prev = current

    enriched = {dict(r)["code"]: dict(r) for r in pool_rows}
    result = []
    for (iso, code), base in row_by_key.items():
        if iso != date_str:
            continue
        extra = enriched.get(code)
        if extra:
            base.update({k: extra.get(k, base.get(k)) for k in (
                "name", "industry", "market_cap", "turnover_rate", "seal_amount",
                "first_seal_time", "last_seal_time", "blast_count"
            )})
            base["source"] = "daily_stk_limit+limit_up_pool"
        result.append(base)
    result.sort(key=lambda r: (-int(r.get("streak") or 0), r["code"]))
    return result


def read_s5_universe(date_str: str) -> tuple[list[str], dict[str, str]]:
    conn = get_market_db()
    try:
        rows = conn.execute(
            "SELECT code, industry FROM s5_daily_universe WHERE date = ? ORDER BY code",
            (date_str,),
        ).fetchall()
    finally:
        conn.close()
    return [r["code"] for r in rows], {r["code"]: r["industry"] for r in rows}


def read_recent_limit_up_universe(date_str: str, lookback_days: int = 30) -> tuple[list[str], dict[str, str]]:
    """Build a no-future stock universe from stocks that limit-upped in [T-N, T]."""
    start = shift_trading_days(date_str, -lookback_days)
    start_raw = iso_to_yyyymmdd(start)
    end_raw = iso_to_yyyymmdd(date_str)
    conn = get_market_db()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT substr(d.ts_code, 1, 6) AS code
            FROM daily d
            INNER JOIN stk_limit l ON l.trade_date = d.trade_date AND l.ts_code = d.ts_code
            WHERE d.trade_date >= ? AND d.trade_date <= ?
              AND l.up_limit IS NOT NULL
              AND d.close >= l.up_limit - 0.01
            ORDER BY code
            """,
            (start_raw, end_raw),
        ).fetchall()
    finally:
        conn.close()
    codes = [r["code"] for r in rows]
    industries = read_latest_industries(codes, as_of=date_str)
    return codes, industries


def read_latest_industries(codes: list[str], as_of: str | None = None) -> dict[str, str]:
    if not codes:
        return {}
    uniq = sorted(set(codes))
    params: list = list(uniq)
    date_clause = ""
    if as_of is not None:
        date_clause = "AND date <= ?"
        params.append(as_of)
    conn = get_market_db()
    try:
        placeholders = ",".join("?" * len(uniq))
        rows = conn.execute(
            f"""
            SELECT p.code, p.industry
            FROM limit_up_pool p
            INNER JOIN (
                SELECT code, MAX(date) AS max_date
                FROM limit_up_pool
                WHERE code IN ({placeholders}) AND industry IS NOT NULL {date_clause}
                GROUP BY code
            ) latest ON latest.code = p.code AND latest.max_date = p.date
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    return {r["code"]: r["industry"] for r in rows if r["industry"]}


def read_latest_names(codes: list[str], as_of: str | None = None) -> dict[str, str]:
    if not codes:
        return {}
    uniq = sorted(set(codes))
    params: list = list(uniq)
    date_clause = ""
    if as_of is not None:
        date_clause = "AND date <= ?"
        params.append(as_of)
    conn = get_market_db()
    try:
        placeholders = ",".join("?" * len(uniq))
        rows = conn.execute(
            f"""
            SELECT p.code, p.name
            FROM limit_up_pool p
            INNER JOIN (
                SELECT code, MAX(date) AS max_date
                FROM limit_up_pool
                WHERE code IN ({placeholders}) AND name IS NOT NULL {date_clause}
                GROUP BY code
            ) latest ON latest.code = p.code AND latest.max_date = p.date
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    return {r["code"]: r["name"] for r in rows if r["name"]}


def resolve_names(codes, as_of: str | None = None) -> dict[str, str]:
    """6位 code → 当前股名(含 *ST/ST/退 标记). 各战法选股写库前据此回填真名并剔 ST.

    主源 stock_names(tushare 全市场, refresh_stock_names.py 维护); 缺的 code 退回
    limit_up_pool / kpl_theme_daily 兜底. ST 状态慢变, stock_names 取当前快照不分日期.
    """
    if not codes:
        return {}
    uniq = sorted({str(x).split(".")[0].zfill(6) for x in codes})
    out: dict[str, str] = {}
    conn = get_market_db()
    try:
        ph = ",".join("?" * len(uniq))
        # 主源: stock_names
        try:
            for r in conn.execute(
                f"SELECT code, name FROM stock_names WHERE code IN ({ph})", uniq
            ):
                if r["name"]:
                    out[r["code"]] = r["name"]
        except sqlite3.OperationalError:
            pass  # stock_names 尚未建表, 走兜底
        # 兜底1: limit_up_pool (point-in-time)
        missing = [c for c in uniq if c not in out]
        if missing:
            mph = ",".join("?" * len(missing))
            params = list(missing)
            date_clause = ""
            if as_of is not None:
                date_clause = "AND date <= ?"
                params.append(as_of)
            for r in conn.execute(
                f"""SELECT p.code, p.name FROM limit_up_pool p
                    INNER JOIN (SELECT code, MAX(date) md FROM limit_up_pool
                                WHERE code IN ({mph}) AND name IS NOT NULL {date_clause}
                                GROUP BY code) l
                    ON l.code=p.code AND l.md=p.date""",
                params,
            ):
                if r["name"]:
                    out[r["code"]] = r["name"]
        # 兜底2: kpl_theme_daily.stock_name (按 ts_code 前缀)
        missing = [c for c in uniq if c not in out]
        if missing:
            for code in missing:
                r = conn.execute(
                    "SELECT stock_name FROM kpl_theme_daily WHERE ts_code LIKE ? "
                    "AND stock_name IS NOT NULL ORDER BY trade_date DESC LIMIT 1",
                    (code + "%",),
                ).fetchone()
                if r and r["stock_name"]:
                    out[code] = r["stock_name"]
    finally:
        conn.close()
    return out


def read_klines(codes: list[str], start: str, end: str) -> dict[str, list[dict]]:
    if not codes:
        return {}

    start_raw = iso_to_yyyymmdd(start)
    end_raw = iso_to_yyyymmdd(end)
    conn = get_market_db()
    try:
        placeholders = ",".join("?" * len(set(codes)))
        rows = conn.execute(
            f"""
            SELECT d.ts_code, d.trade_date, d.open, d.high, d.low, d.close,
                   d.pre_close, d.pct_chg, d.vol, d.amount,
                   l.up_limit, l.down_limit
            FROM daily d
            LEFT JOIN stk_limit l ON d.trade_date = l.trade_date AND d.ts_code = l.ts_code
            WHERE substr(d.ts_code, 1, 6) IN ({placeholders})
              AND d.trade_date >= ? AND d.trade_date <= ?
            ORDER BY d.ts_code, d.trade_date
            """,
            (*sorted(set(codes)), start_raw, end_raw),
        ).fetchall()
    finally:
        conn.close()

    result: dict[str, list[dict]] = {}
    for r in rows:
        code = r["ts_code"][:6]
        up_limit = r["up_limit"]
        down_limit = r["down_limit"]
        close = float(r["close"] or 0)
        bar = {
            "date": yyyymmdd_to_iso(r["trade_date"]),
            "open": float(r["open"] or 0),
            "high": float(r["high"] or 0),
            "low": float(r["low"] or 0),
            "close": close,
            "pre_close": float(r["pre_close"] or 0),
            "pct_chg": float(r["pct_chg"] or 0),
            "volume": float(r["vol"] or 0) * 100,
            "amount": float(r["amount"] or 0) * 1000,
            "up_limit": float(up_limit) if up_limit is not None else None,
            "down_limit": float(down_limit) if down_limit is not None else None,
            "is_limit_up": bool(up_limit is not None and close >= float(up_limit) - 0.01),
            "is_limit_down": bool(down_limit is not None and close <= float(down_limit) + 0.01),
        }
        result.setdefault(code, []).append(bar)
    return result


def fetch_followup_bars(code: str, t1_date: str, n_days: int) -> list[dict]:
    start = t1_date
    end = shift_trading_days(t1_date, max(n_days - 1, 0))
    return read_klines([code], start, end).get(code, [])[:n_days]


def moving_average(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window
