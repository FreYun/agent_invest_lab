"""SQLite 连接 + schema 定义。

数据库路径: /home/rooot/agent_invest_lab/data/market.db
该路径不在任何 git 仓库内, 数据不会被跟踪。

表:
    daily          — 全市场股票日线 (~5000 行/天)
    stk_limit      — 全市场涨跌停价 (~5000 行/天)
    index_daily    — 主要指数日线 (4 个指数)
    backfill_progress — 回填进度 (支持增量)
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime

DB_PATH = os.environ.get("MARKET_DB_PATH") or os.environ.get("SCOUT_DB_PATH") or "/home/rooot/agent_invest_lab/data/market.db"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS daily (
    trade_date TEXT NOT NULL,
    ts_code TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    pre_close REAL, pct_chg REAL,
    vol REAL, amount REAL,
    PRIMARY KEY (trade_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_daily_date ON daily(trade_date);

CREATE TABLE IF NOT EXISTS stk_limit (
    trade_date TEXT NOT NULL,
    ts_code TEXT NOT NULL,
    up_limit REAL,
    down_limit REAL,
    PRIMARY KEY (trade_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_stk_limit_date ON stk_limit(trade_date);

CREATE TABLE IF NOT EXISTS index_daily (
    trade_date TEXT NOT NULL,
    ts_code TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    pre_close REAL, pct_chg REAL,
    vol REAL, amount REAL,
    PRIMARY KEY (trade_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_index_daily_date ON index_daily(trade_date);

CREATE TABLE IF NOT EXISTS backfill_progress (
    task TEXT PRIMARY KEY,
    last_completed_date TEXT,
    updated_at TEXT
);
"""


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    """打开 SQLite 连接, 首次调用自动建表。

    使用 WAL 模式 + 较大 cache 提高写入吞吐 (backfill 一次 ~3.6M 行)。
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.executescript(SCHEMA_SQL)
    # 性能调优: WAL + 内存页缓存
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-20000")  # 20 MB
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def get_progress(conn: sqlite3.Connection, task: str) -> str | None:
    """返回某 task 最后一次成功回填到的日期 (YYYYMMDD), 未开始则 None."""
    row = conn.execute(
        "SELECT last_completed_date FROM backfill_progress WHERE task = ?",
        (task,),
    ).fetchone()
    return row[0] if row else None


def set_progress(conn: sqlite3.Connection, task: str, last_date: str) -> None:
    """更新 task 的回填进度。"""
    conn.execute(
        """
        INSERT INTO backfill_progress(task, last_completed_date, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(task) DO UPDATE SET
            last_completed_date = excluded.last_completed_date,
            updated_at = excluded.updated_at
        """,
        (task, last_date, datetime.now().isoformat()),
    )
    conn.commit()


def date_exists(conn: sqlite3.Connection, table: str, trade_date: str) -> bool:
    """检查某天数据是否已经在表里 (用于跳过已拉取的日期)."""
    assert table in ("daily", "stk_limit", "index_daily"), f"unknown table: {table}"
    row = conn.execute(
        f"SELECT 1 FROM {table} WHERE trade_date = ? LIMIT 1", (trade_date,)
    ).fetchone()
    return row is not None


def row_counts(conn: sqlite3.Connection) -> dict:
    """返回各表行数 (用于诊断)."""
    result = {}
    for t in ("daily", "stk_limit", "index_daily"):
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        result[t] = n
    return result


def date_range(conn: sqlite3.Connection, table: str) -> tuple[str | None, str | None]:
    """返回某表的 [min_trade_date, max_trade_date] (字符串, 空表返回 None)."""
    row = conn.execute(
        f"SELECT MIN(trade_date), MAX(trade_date) FROM {table}"
    ).fetchone()
    return row[0], row[1]
