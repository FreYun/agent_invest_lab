"""从 market.db 的 regime_classify_daily 读 regime, 组装成 classifier JSON 同构 dict.

下游 (candidate_builder / select.run_select) 消费的是 classifier JSON 里的:
    regime_code, regime, confidence, switched, emergency_switch,
    score.total, playbook.recommended, playbook.position_limit.single

DB 表只存前 6 项分类结果, 不存 playbook (playbook 是 regime_code → mapping 的派生).
这里 import classifier 的 lookup_playbook 补全, 保持唯一来源.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from typing import Optional

_MARKET_REGIME_SCRIPTS = "/home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/market-regime/scripts"
if _MARKET_REGIME_SCRIPTS not in sys.path:
    sys.path.insert(0, _MARKET_REGIME_SCRIPTS)

from regime_rules import lookup_playbook  # noqa: E402

DB_PATH = __import__("os").environ.get("MARKET_DB_PATH", __import__("os").environ.get("SCOUT_DB_PATH", "/home/rooot/agent_invest_lab/data/market.db"))


class RegimeNotFound(Exception):
    pass


def load_regime_from_db(
    trade_date: str,
    rules_version: str = "v2",
    db_path: str = DB_PATH,
) -> dict:
    """查 regime_classify_daily, 组装成 classifier JSON 同构 dict.

    Args:
        trade_date: 'YYYY-MM-DD'
        rules_version: 'v1' 或 'v2', 默认 v2 (pipeline.sh 日更的版本)
        db_path: market.db 路径

    Raises:
        RegimeNotFound: 表里没有该日记录
    """
    conn = sqlite3.connect(db_path)
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

    regime_code = row["regime_code"]
    playbook = lookup_playbook(regime_code)

    missing_dims_raw = row["missing_dims"] or ""
    missing_dims = [d for d in missing_dims_raw.split(";") if d]

    return {
        "date": row["trade_date"],
        "regime": row["regime_name"],
        "regime_code": regime_code,
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
        "missing_dims": missing_dims,
        "last_regime_code": row["last_regime_code"],
        "switched": bool(row["switched"]),
        "bootstrap": bool(row["bootstrap"]),
        "emergency_switch": bool(row["emergency_switch"]),
        "emergency_direction": row["emergency_direction"],
        "emergency_reason": (row["emergency_reason"] or "").split(";")
        if row["emergency_reason"]
        else [],
        "playbook": playbook,
        "source": {"kind": "db", "rules_version": rules_version},
    }
