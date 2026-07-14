"""strategy 共享 DB 层 — 提供 market.db 连接 + S5 相关表 schema.

所有 S5 脚本 (s5-prewarm.py / select.py / data_fetcher.py / db_writer.py)
通过 sys.path 引用本模块, 调用 init_market_db() 建表 + get_market_db() 拿连接.
"""

from __future__ import annotations

import os
import sqlite3

DB_DIR = "/home/rooot/agent_invest_lab/data"
DB_PATH = os.environ.get("MARKET_DB_PATH") or os.environ.get("SCOUT_DB_PATH") or os.path.join(DB_DIR, "market.db")

_SCHEMA_S5 = """
CREATE TABLE IF NOT EXISTS limit_up_pool (
    date             TEXT NOT NULL,
    code             TEXT NOT NULL,
    name             TEXT,
    industry         TEXT,
    pct_chg          REAL,
    streak           INTEGER,
    close            REAL,
    amount           REAL,
    market_cap       REAL,
    turnover_rate    REAL,
    seal_amount      REAL,
    first_seal_time  TEXT,
    last_seal_time   TEXT,
    blast_count      INTEGER,
    cached_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date, code)
);

CREATE TABLE IF NOT EXISTS klines_cache (
    code               TEXT NOT NULL,
    date               TEXT NOT NULL,
    open               REAL,
    high               REAL,
    low                REAL,
    close              REAL,
    pct_chg            REAL,
    volume             REAL,
    amount             REAL,
    is_limit_up        INTEGER,
    is_limit_down      INTEGER,
    limit_up_streak    INTEGER,
    limit_down_streak  INTEGER,
    cached_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS hot_industries_daily (
    date          TEXT NOT NULL,
    rank          INTEGER NOT NULL,
    industry      TEXT NOT NULL,
    limit_count   INTEGER NOT NULL,
    PRIMARY KEY (date, industry)
);

CREATE TABLE IF NOT EXISTS s5_daily_universe (
    date       TEXT NOT NULL,
    code       TEXT NOT NULL,
    industry   TEXT,
    PRIMARY KEY (date, code)
);

CREATE TABLE IF NOT EXISTS s5_select_runs (
    date                        TEXT PRIMARY KEY,
    strategy                    TEXT NOT NULL,
    regime_code                 TEXT NOT NULL,
    regime_name                 TEXT NOT NULL,
    regime_score                INTEGER NOT NULL,
    confidence                  TEXT,
    switched                    INTEGER,
    emergency_switch            INTEGER,
    position_limit_single_base  REAL,
    skipped_reason              TEXT,
    regime_gate_allowed         INTEGER,
    regime_gate_reason          TEXT,
    universe_size               INTEGER,
    dragon_pool_size            INTEGER,
    passed_count                INTEGER,
    hot_industries_json         TEXT,
    config_json                 TEXT,
    created_at                  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS s5_candidates (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    date                      TEXT NOT NULL,
    code                      TEXT NOT NULL,
    name                      TEXT,
    industry                  TEXT,
    dragon_peak_date          TEXT NOT NULL,
    dragon_peak_close         REAL NOT NULL,
    dragon_peak_max_streak    INTEGER NOT NULL,
    cooldown_days             INTEGER NOT NULL,
    cooldown_drop_pct         REAL NOT NULL,
    cooldown_t1_close         REAL,
    rebound_t_pct             REAL NOT NULL,
    rebound_t_close           REAL NOT NULL,
    rebound_t_low             REAL,
    rebound_t_high            REAL,
    rebound_t1_high           REAL,
    entry_zone_low            REAL NOT NULL,
    entry_zone_high           REAL NOT NULL,
    entry_rule                TEXT,
    stop_loss_price           REAL NOT NULL,
    stop_loss_rule            TEXT,
    target_1_price            REAL,
    target_2_price            REAL,
    position_pct              REAL NOT NULL,
    position_calc             TEXT,
    UNIQUE(date, code)
);

CREATE TABLE IF NOT EXISTS s5_candidate_rejects (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    date           TEXT NOT NULL,
    code           TEXT NOT NULL,
    name           TEXT,
    stage_failed   TEXT NOT NULL,
    reject_reason  TEXT NOT NULL,
    UNIQUE(date, code)
);

CREATE TABLE IF NOT EXISTS s5_verifications (
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
    live_open_price   REAL,
    live_current      REAL,
    hold_days         INTEGER,
    exit_date         TEXT,
    note              TEXT,
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(candidate_id) REFERENCES s5_candidates(id)
);
"""


def init_market_db(db_path: str = DB_PATH) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.executescript(_SCHEMA_S5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()


def get_market_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn
