"""run_id 列 + 快照表 PK 重建 的迁移单测。
模拟一个老版本 schema 的 db（没有 run_id 列、快照表 PK 不含 run_id），
跑 init_db() 后验证：
  - 5 张表加上 run_id 列（旧行 NULL）
  - 2 张快照表 PK 含 run_id（旧行 run_id='' 保留）
  - orders 表拆出 order_run_id / settle_run_id
"""
import os
import sqlite3
import tempfile
import pytest


OLD_SCHEMA = """
CREATE TABLE fund_bot_accounts (
    bot_id TEXT PRIMARY KEY, initial_capital REAL NOT NULL, cash REAL NOT NULL,
    cash_in_transit REAL NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE fund_bot_holdings (
    holding_id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL, fund_code TEXT NOT NULL,
    fund_name TEXT, share_class TEXT, asset_class TEXT, role TEXT,
    entry_date TEXT, exit_date TEXT, entry_nav REAL, latest_nav REAL,
    shares REAL, pending_sell_shares REAL NOT NULL DEFAULT 0, amount_invested REAL,
    market_value REAL, unrealized_pnl REAL, unrealized_pnl_pct REAL,
    target_weight REAL, actual_weight REAL, holding_days INTEGER, high_nav REAL,
    status TEXT DEFAULT 'active', thesis TEXT
);
CREATE TABLE fund_bot_orders (
    order_id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER, bot_id TEXT NOT NULL,
    fund_code TEXT NOT NULL, fund_name TEXT, order_type TEXT NOT NULL, order_date TEXT NOT NULL,
    confirm_date TEXT, order_amount REAL, reference_nav REAL, confirm_nav REAL,
    confirmed_shares REAL, confirmed_amount REAL, fee REAL, action_reason TEXT,
    status TEXT DEFAULT 'pending', created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE fund_bot_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT, review_date TEXT,
    regime TEXT, decision TEXT, action_count INTEGER, reason TEXT, review_md TEXT,
    cooldown_end TEXT, cash_before REAL, cash_after REAL,
    portfolio_value_before REAL, portfolio_value_after REAL,
    turnover_amount REAL, turnover_ratio REAL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE fund_bot_actions (
    action_id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER, bot_id TEXT,
    fund_code TEXT, action_type TEXT, trigger TEXT, timing_state TEXT, momentum_state TEXT,
    matrix_suggestion TEXT, final_decision TEXT, before_weight REAL, after_weight REAL,
    nav_used REAL, amount REAL, shares REAL, fee REAL, reason TEXT, action_date TEXT
);
CREATE TABLE fund_bot_daily_snapshots (
    bot_id TEXT NOT NULL, trade_date TEXT NOT NULL,
    initial_capital REAL, cash REAL, invested_value REAL, total_value REAL,
    net_value REAL, daily_return_pct REAL, cumulative_return_pct REAL,
    max_drawdown_pct REAL, equity_weight REAL, bond_weight REAL,
    gold_weight REAL, cash_weight REAL, holdings_json TEXT,
    PRIMARY KEY (bot_id, trade_date)
);
CREATE TABLE fund_bot_position_snapshots (
    bot_id TEXT NOT NULL, fund_code TEXT NOT NULL, trade_date TEXT NOT NULL,
    asset_class TEXT, role TEXT, shares REAL, nav REAL, market_value REAL,
    weight REAL, daily_pnl REAL, cumulative_return_pct REAL, holding_days INTEGER,
    PRIMARY KEY (bot_id, fund_code, trade_date)
);
"""


@pytest.fixture
def legacy_db(monkeypatch):
    """造一个老版本 schema 的 db 并塞几条数据，模拟生产升级现场。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO fund_bot_accounts (bot_id, initial_capital, cash) VALUES (?, ?, ?)",
        ("bot7", 1_000_000.0, 1_000_000.0),
    )
    conn.execute(
        "INSERT INTO fund_bot_reviews (bot_id, review_date, regime, decision) VALUES (?, ?, ?, ?)",
        ("bot7", "2026-04-15", "range", "KEEP"),
    )
    conn.execute(
        "INSERT INTO fund_bot_orders (bot_id, fund_code, order_type, order_date, order_amount, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("bot7", "001234", "buy", "2026-04-15", 50_000.0, "confirmed"),
    )
    conn.execute(
        "INSERT INTO fund_bot_daily_snapshots (bot_id, trade_date, cash, total_value) "
        "VALUES (?, ?, ?, ?)",
        ("bot7", "2026-04-15", 950_000.0, 1_000_000.0),
    )
    conn.execute(
        "INSERT INTO fund_bot_position_snapshots (bot_id, fund_code, trade_date, shares, nav) "
        "VALUES (?, ?, ?, ?, ?)",
        ("bot7", "001234", "2026-04-15", 100.0, 1.234),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("db.DB_PATH", path)
    yield path
    os.unlink(path)


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _pk_columns(conn, table):
    return tuple(
        r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall() if r[5] > 0
    )


def test_migration_adds_run_id_to_simple_tables(legacy_db):
    import db as db_mod
    db_mod.init_db()
    conn = sqlite3.connect(legacy_db)
    assert "run_id" in _columns(conn, "fund_bot_accounts")
    assert "run_id" in _columns(conn, "fund_bot_holdings")
    assert "run_id" in _columns(conn, "fund_bot_reviews")
    assert "run_id" in _columns(conn, "fund_bot_actions")
    # orders 拆两列
    cols = _columns(conn, "fund_bot_orders")
    assert "order_run_id" in cols
    assert "settle_run_id" in cols
    conn.close()


def test_migration_rebuilds_snapshot_pks(legacy_db):
    import db as db_mod
    db_mod.init_db()
    conn = sqlite3.connect(legacy_db)
    assert _pk_columns(conn, "fund_bot_daily_snapshots") == ("bot_id", "trade_date", "run_id")
    assert _pk_columns(conn, "fund_bot_position_snapshots") == ("bot_id", "fund_code", "trade_date", "run_id")
    conn.close()


def test_migration_preserves_old_rows(legacy_db):
    """老行不丢，run_id 是 NULL（5 张直加列表）或 ''（2 张快照表，PK 默认值）。"""
    import db as db_mod
    db_mod.init_db()
    conn = sqlite3.connect(legacy_db)
    # accounts
    row = conn.execute("SELECT bot_id, run_id FROM fund_bot_accounts WHERE bot_id=?", ("bot7",)).fetchone()
    assert row is not None
    assert row[1] is None  # 旧行 run_id 是 NULL
    # reviews
    row = conn.execute("SELECT decision, run_id FROM fund_bot_reviews WHERE bot_id=?", ("bot7",)).fetchone()
    assert row[0] == "KEEP"
    assert row[1] is None
    # orders
    row = conn.execute(
        "SELECT order_type, order_run_id, settle_run_id FROM fund_bot_orders WHERE bot_id=?",
        ("bot7",),
    ).fetchone()
    assert row[0] == "buy"
    assert row[1] is None and row[2] is None
    # daily snapshot：旧行 run_id 应为空字符串（PK 默认值，NOT NULL）
    row = conn.execute(
        "SELECT trade_date, run_id, total_value FROM fund_bot_daily_snapshots WHERE bot_id=?",
        ("bot7",),
    ).fetchone()
    assert row is not None
    assert row[1] == ""
    assert row[2] == 1_000_000.0
    # position snapshot
    row = conn.execute(
        "SELECT fund_code, run_id, shares FROM fund_bot_position_snapshots WHERE bot_id=?",
        ("bot7",),
    ).fetchone()
    assert row[0] == "001234"
    assert row[1] == ""
    assert row[2] == 100.0
    conn.close()


def test_migration_idempotent(legacy_db):
    """重跑 init_db 不应失败、不应丢数据、不应重复加列。"""
    import db as db_mod
    db_mod.init_db()
    db_mod.init_db()  # 再来一次
    db_mod.init_db()  # 再来一次
    conn = sqlite3.connect(legacy_db)
    # 列数应只有一份 run_id
    cols = _columns(conn, "fund_bot_accounts")
    assert cols.count("run_id") == 1
    # 数据未丢
    cnt = conn.execute("SELECT COUNT(*) FROM fund_bot_reviews").fetchone()[0]
    assert cnt == 1
    conn.close()


def test_same_day_multiple_runs_in_daily_snapshot(legacy_db):
    """迁移后，同一 (bot,trade_date) 不同 run_id 可共存（PK 含 run_id）。"""
    import db as db_mod
    db_mod.init_db()
    conn = sqlite3.connect(legacy_db)
    conn.execute(
        "INSERT INTO fund_bot_daily_snapshots (bot_id, trade_date, run_id, total_value) "
        "VALUES (?, ?, ?, ?)",
        ("bot7", "2026-04-15", "run-A", 1_010_000.0),
    )
    conn.execute(
        "INSERT INTO fund_bot_daily_snapshots (bot_id, trade_date, run_id, total_value) "
        "VALUES (?, ?, ?, ?)",
        ("bot7", "2026-04-15", "run-B", 1_020_000.0),
    )
    conn.commit()
    rows = conn.execute(
        "SELECT run_id, total_value FROM fund_bot_daily_snapshots "
        "WHERE bot_id=? AND trade_date=? ORDER BY run_id",
        ("bot7", "2026-04-15"),
    ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [
        ("", 1_000_000.0),
        ("run-A", 1_010_000.0),
        ("run-B", 1_020_000.0),
    ]
    conn.close()
