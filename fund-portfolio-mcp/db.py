"""
SQLite 数据库初始化 + 连接管理

数据库文件: /home/rooot/.openclaw/data/fund.db
模式: WAL (Write-Ahead Logging) for concurrent reads

基金数据侧 6 表（刷新脚本写入，bot 只读）:
  fund_info, fund_nav, fund_performance, fund_style, fund_industry, fund_top_stocks

Bot 执行侧 7 表（MCP 读写）:
  fund_bot_accounts, fund_bot_holdings, fund_bot_orders, fund_bot_reviews,
  fund_bot_actions, fund_bot_daily_snapshots, fund_bot_position_snapshots

系统表 3 表:
  fund_system_runs, fund_allocation_runs, fund_selection_runs
"""

import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.environ.get(
    "FUND_DB_PATH",
    os.path.join(
        os.environ.get("OPENCLAW_ROOT", "/home/rooot/agent_invest_lab"),
        "data",
        "fund.db",
    ),
)

SCHEMA_SQL = """
-- ============================================================
-- 基金数据侧（刷新脚本写入，bot 只读）
-- ============================================================

-- 1. 基金主表
CREATE TABLE IF NOT EXISTS fund_info (
    fund_code           TEXT PRIMARY KEY,
    fund_name           TEXT NOT NULL,
    fund_company        TEXT,
    fund_manager        TEXT,
    fund_type           TEXT,
    share_class         TEXT,
    established_date    TEXT,
    scale               REAL,
    purchase_status     TEXT,
    redeem_status       TEXT,
    mgmt_fee            REAL,
    custody_fee         REAL,
    purchase_fee        REAL,
    sales_service_fee   REAL,
    redeem_fee_json     TEXT,
    theme               TEXT,  -- 近一年主题（来自核心池 excel；固收类为 NULL）
    updated_at          TEXT
);

-- 2. 每日净值
CREATE TABLE IF NOT EXISTS fund_nav (
    fund_code           TEXT NOT NULL,
    nav_date            TEXT NOT NULL,
    nav                 REAL,
    acc_nav             REAL,
    daily_return_pct    REAL,
    updated_at          TEXT,
    PRIMARY KEY (fund_code, nav_date)
);

-- 3. 多区间业绩
CREATE TABLE IF NOT EXISTS fund_performance (
    fund_code           TEXT NOT NULL,
    as_of_date          TEXT NOT NULL,
    period              TEXT NOT NULL,
    return_pct          REAL,
    rank_pct            REAL,
    rank_text           TEXT,
    max_drawdown_pct    REAL,
    volatility_pct      REAL,
    sharpe_ratio        REAL,
    calmar_ratio        REAL,
    updated_at          TEXT,
    PRIMARY KEY (fund_code, as_of_date, period)
);

-- 4. 风格分析
CREATE TABLE IF NOT EXISTS fund_style (
    fund_code           TEXT NOT NULL,
    as_of_date          TEXT NOT NULL,
    size_style          TEXT,
    invest_style        TEXT,
    equity_pct          REAL,
    bond_pct            REAL,
    cash_pct            REAL,
    other_pct           REAL,
    updated_at          TEXT,
    PRIMARY KEY (fund_code, as_of_date)
);

-- 5. 行业持仓
CREATE TABLE IF NOT EXISTS fund_industry (
    fund_code           TEXT NOT NULL,
    as_of_date          TEXT NOT NULL,
    industry            TEXT NOT NULL,
    weight_pct          REAL,
    updated_at          TEXT,
    PRIMARY KEY (fund_code, as_of_date, industry)
);

-- 6. 重仓股
CREATE TABLE IF NOT EXISTS fund_top_stocks (
    fund_code           TEXT NOT NULL,
    as_of_date          TEXT NOT NULL,
    stock_rank          INTEGER,
    stock_code          TEXT NOT NULL,
    stock_name          TEXT,
    weight_pct          REAL,
    updated_at          TEXT,
    PRIMARY KEY (fund_code, as_of_date, stock_code)
);

-- ============================================================
-- Bot 执行侧（MCP 读写）
-- ============================================================

-- 7. Bot 账户主表
--    cash             = 可用现金（available）
--    cash_in_transit  = pending BUY 冻结的现金；T+1 settle 时从 in_transit 扣减并下账
--    run_id           = 最近一次写入本行的 run_id（init_fund_account 时也填）。审计用。
CREATE TABLE IF NOT EXISTS fund_bot_accounts (
    bot_id              TEXT PRIMARY KEY,
    initial_capital     REAL NOT NULL,
    cash                REAL NOT NULL,
    cash_in_transit     REAL NOT NULL DEFAULT 0,
    run_id              TEXT,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);

-- 8. Bot 持仓表
--    shares                = 持仓总份额（含被 pending sell 锁住的份额）
--    pending_sell_shares   = pending SELL 冻结的份额；可下卖单的额度 = shares - pending_sell_shares
CREATE TABLE IF NOT EXISTS fund_bot_holdings (
    holding_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id              TEXT NOT NULL,
    fund_code           TEXT NOT NULL,
    fund_name           TEXT,
    share_class         TEXT,
    asset_class         TEXT,
    role                TEXT,
    entry_date          TEXT,
    exit_date           TEXT,
    entry_nav           REAL,
    latest_nav          REAL,
    shares              REAL,
    pending_sell_shares REAL NOT NULL DEFAULT 0,
    amount_invested     REAL,
    market_value        REAL,
    unrealized_pnl      REAL,
    unrealized_pnl_pct  REAL,
    target_weight       REAL,
    actual_weight       REAL,
    holding_days        INTEGER,
    high_nav            REAL,
    status              TEXT DEFAULT 'active',
    thesis              TEXT,
    run_id              TEXT  -- 最近一次写入本行的 run_id
);

-- 9. 在途订单表（基金直投特有，T+1 结算）
-- run_id 语义：order_run_id = 下单那一轮 run；settle_run_id = T+1 收口那一轮 run。
-- 两者通常是不同 run（不同的 trade_date / pi-loop 回合），都保留以便审计。
CREATE TABLE IF NOT EXISTS fund_bot_orders (
    order_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id           INTEGER,
    bot_id              TEXT NOT NULL,
    fund_code           TEXT NOT NULL,
    fund_name           TEXT,
    order_type          TEXT NOT NULL,
    order_date          TEXT NOT NULL,
    confirm_date        TEXT,
    order_amount        REAL,
    reference_nav       REAL,
    confirm_nav         REAL,
    confirmed_shares    REAL,
    confirmed_amount    REAL,
    fee                 REAL,
    action_reason       TEXT,
    status              TEXT DEFAULT 'pending',
    order_run_id        TEXT,
    settle_run_id       TEXT,
    created_at          TEXT DEFAULT (datetime('now'))
);

-- 10. Bot 巡检记录
CREATE TABLE IF NOT EXISTS fund_bot_reviews (
    review_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id                  TEXT,
    review_date             TEXT,
    regime                  TEXT,
    decision                TEXT,
    action_count            INTEGER,
    reason                  TEXT,
    review_md               TEXT,
    cooldown_end            TEXT,
    cash_before             REAL,
    cash_after              REAL,
    portfolio_value_before  REAL,
    portfolio_value_after   REAL,
    turnover_amount         REAL,
    turnover_ratio          REAL,
    run_id                  TEXT,  -- 巡检发起的 run；同 (bot,date) 可有多条
    created_at              TEXT DEFAULT (datetime('now'))
);

-- 11. Bot 调仓动作表
CREATE TABLE IF NOT EXISTS fund_bot_actions (
    action_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id           INTEGER,
    bot_id              TEXT,
    fund_code           TEXT,
    action_type         TEXT,
    trigger             TEXT,
    timing_state        TEXT,
    momentum_state      TEXT,
    matrix_suggestion   TEXT,
    final_decision      TEXT,
    before_weight       REAL,
    after_weight        REAL,
    nav_used            REAL,
    amount              REAL,
    shares              REAL,
    fee                 REAL,
    reason              TEXT,
    action_date         TEXT,
    run_id              TEXT  -- settle 时落 actions 用的是 settle 那一轮 run_id
);

-- 12. 账户级每日快照
--    PK 含 run_id：同一 (bot,trade_date) 多次 run 各保留一份快照。读侧用「最新 run_id」视图。
--    历史数据迁移时若 run_id 为 NULL，按 (bot,trade_date,'') 落库（SQLite NULL 不参与 UNIQUE）。
CREATE TABLE IF NOT EXISTS fund_bot_daily_snapshots (
    bot_id                  TEXT NOT NULL,
    trade_date              TEXT NOT NULL,
    run_id                  TEXT NOT NULL DEFAULT '',
    initial_capital         REAL,
    cash                    REAL,
    invested_value          REAL,
    total_value             REAL,
    net_value               REAL,
    daily_return_pct        REAL,
    cumulative_return_pct   REAL,
    max_drawdown_pct        REAL,
    equity_weight           REAL,
    bond_weight             REAL,
    gold_weight             REAL,
    cash_weight             REAL,
    holdings_json           TEXT,
    PRIMARY KEY (bot_id, trade_date, run_id)
);

-- 13. 持仓级每日快照
CREATE TABLE IF NOT EXISTS fund_bot_position_snapshots (
    bot_id                  TEXT NOT NULL,
    fund_code               TEXT NOT NULL,
    trade_date              TEXT NOT NULL,
    run_id                  TEXT NOT NULL DEFAULT '',
    asset_class             TEXT,
    role                    TEXT,
    shares                  REAL,
    nav                     REAL,
    market_value            REAL,
    weight                  REAL,
    daily_pnl               REAL,
    cumulative_return_pct   REAL,
    holding_days            INTEGER,
    PRIMARY KEY (bot_id, fund_code, trade_date, run_id)
);

-- ============================================================
-- 系统表
-- ============================================================

-- 14. 执行轮次
CREATE TABLE IF NOT EXISTS fund_system_runs (
    run_id              TEXT PRIMARY KEY,
    trade_date          TEXT NOT NULL,
    data_version        TEXT,
    phase_a_status      TEXT,
    gate_status         TEXT,
    skip_reason         TEXT,
    created_at          TEXT DEFAULT (datetime('now'))
);

-- 15. 大类资产配置结果
CREATE TABLE IF NOT EXISTS fund_allocation_runs (
    allocation_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL,
    bot_id              TEXT NOT NULL,
    trade_date          TEXT NOT NULL,
    regime              TEXT,
    market_summary_md   TEXT,
    asset_target_json   TEXT,
    created_at          TEXT DEFAULT (datetime('now'))
);

-- 16. 选品漏斗追踪（基金特有）
CREATE TABLE IF NOT EXISTS fund_selection_runs (
    selection_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT,
    bot_id              TEXT NOT NULL,
    trade_date          TEXT NOT NULL,
    layer1_count        INTEGER,
    layer2_count        INTEGER,
    layer3_count        INTEGER,
    layer4_count        INTEGER,
    selected_funds_json TEXT,
    eliminated_json     TEXT,
    selection_md        TEXT,
    created_at          TEXT DEFAULT (datetime('now'))
);

-- ============================================================
-- 三层框架表(2026-04-29 新增)
-- ============================================================

-- 17. 能力圈宣告快照
CREATE TABLE IF NOT EXISTS fund_capability_circle (
    circle_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id                 TEXT NOT NULL,
    as_of_date             TEXT NOT NULL,
    macro                  INTEGER NOT NULL DEFAULT 0,
    industry_rotation      INTEGER NOT NULL DEFAULT 0,
    industry_focus         TEXT,
    fund_alpha             INTEGER NOT NULL DEFAULT 0,
    default_paradigm       TEXT,
    secondary_paradigm     TEXT,
    switch_rules_json      TEXT,
    evidence_md            TEXT,
    next_assessment_due    TEXT,
    created_at             TEXT DEFAULT (datetime('now')),
    UNIQUE (bot_id, as_of_date)
);

-- 18. 每日 Phase B-1 输出
CREATE TABLE IF NOT EXISTS fund_paradigm_runs (
    paradigm_run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id                 TEXT NOT NULL,
    trade_date             TEXT NOT NULL,
    run_id                 TEXT,
    paradigm_active        TEXT NOT NULL,
    capability_field       TEXT,
    capability_value       TEXT,
    switched_from          TEXT,
    reason                 TEXT,
    created_at             TEXT DEFAULT (datetime('now')),
    UNIQUE (bot_id, trade_date)
);

-- ============================================================
-- 索引
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_fund_nav_code_date ON fund_nav(fund_code, nav_date);
CREATE INDEX IF NOT EXISTS idx_fund_perf_code_date ON fund_performance(fund_code, as_of_date);
CREATE INDEX IF NOT EXISTS idx_fund_industry_code_date ON fund_industry(fund_code, as_of_date);
CREATE INDEX IF NOT EXISTS idx_fund_top_stocks_code_date ON fund_top_stocks(fund_code, as_of_date);
CREATE INDEX IF NOT EXISTS idx_fund_style_code_date ON fund_style(fund_code, as_of_date);
CREATE INDEX IF NOT EXISTS idx_fund_holdings_bot_status ON fund_bot_holdings(bot_id, status);
CREATE INDEX IF NOT EXISTS idx_fund_orders_bot_status ON fund_bot_orders(bot_id, status);
CREATE INDEX IF NOT EXISTS idx_fund_orders_bot_date ON fund_bot_orders(bot_id, order_date);
CREATE INDEX IF NOT EXISTS idx_fund_reviews_bot_date ON fund_bot_reviews(bot_id, review_date);
CREATE INDEX IF NOT EXISTS idx_fund_actions_review ON fund_bot_actions(review_id);
CREATE INDEX IF NOT EXISTS idx_fund_snapshots_bot_date ON fund_bot_daily_snapshots(bot_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_fund_pos_snapshots_bot_date ON fund_bot_position_snapshots(bot_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_fund_system_runs_date ON fund_system_runs(trade_date, created_at);
CREATE INDEX IF NOT EXISTS idx_fund_alloc_runs_bot_date ON fund_allocation_runs(bot_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_fund_alloc_runs_run_id ON fund_allocation_runs(run_id);
CREATE INDEX IF NOT EXISTS idx_fund_selection_runs_bot_date ON fund_selection_runs(bot_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_fund_capability_bot_date ON fund_capability_circle(bot_id, as_of_date);
CREATE INDEX IF NOT EXISTS idx_fund_paradigm_bot_date ON fund_paradigm_runs(bot_id, trade_date);
-- run_id 相关索引在 _migrate_run_id_columns 末尾建（依赖加列动作先跑完，老库才有这些列）。
"""

# 加列后再建 run_id 索引（旧库走 ALTER TABLE ADD COLUMN，索引依赖这些列存在）。
_RUN_ID_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_fund_reviews_run ON fund_bot_reviews(bot_id, review_date, run_id);
CREATE INDEX IF NOT EXISTS idx_fund_actions_run ON fund_bot_actions(bot_id, action_date, run_id);
CREATE INDEX IF NOT EXISTS idx_fund_orders_order_run ON fund_bot_orders(bot_id, order_date, order_run_id);
CREATE INDEX IF NOT EXISTS idx_fund_orders_settle_run ON fund_bot_orders(bot_id, settle_run_id);
CREATE INDEX IF NOT EXISTS idx_fund_holdings_run ON fund_bot_holdings(bot_id, run_id);
"""


def _column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _migrate_paradigm_columns(conn):
    """给 3 张老表加 paradigm 列(如果未加过)。SQLite 不支持 IF NOT EXISTS for ADD COLUMN。"""
    for table in ("fund_bot_reviews", "fund_bot_actions", "fund_allocation_runs"):
        if not _column_exists(conn, table, "paradigm"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN paradigm TEXT")


def _migrate_freeze_columns(conn):
    """新增 cash_in_transit / pending_sell_shares 字段，支持 pending 期间的资金/份额冻结。"""
    if not _column_exists(conn, "fund_bot_accounts", "cash_in_transit"):
        conn.execute("ALTER TABLE fund_bot_accounts ADD COLUMN cash_in_transit REAL NOT NULL DEFAULT 0")
    if not _column_exists(conn, "fund_bot_holdings", "pending_sell_shares"):
        conn.execute("ALTER TABLE fund_bot_holdings ADD COLUMN pending_sell_shares REAL NOT NULL DEFAULT 0")


def _migrate_run_id_columns(conn):
    """7 张执行表加 run_id 审计字段。
       - 5 张直接 ALTER TABLE ADD COLUMN（旧行 NULL）。
       - 2 张快照表 PK 要重建（SQLite 不支持改 PK），用 rename+recreate+copy+drop 的方式迁移。
       - orders 表特殊：拆 order_run_id / settle_run_id 两列。
    """
    # 5 张直加列
    if not _column_exists(conn, "fund_bot_accounts", "run_id"):
        conn.execute("ALTER TABLE fund_bot_accounts ADD COLUMN run_id TEXT")
    if not _column_exists(conn, "fund_bot_holdings", "run_id"):
        conn.execute("ALTER TABLE fund_bot_holdings ADD COLUMN run_id TEXT")
    if not _column_exists(conn, "fund_bot_reviews", "run_id"):
        conn.execute("ALTER TABLE fund_bot_reviews ADD COLUMN run_id TEXT")
    if not _column_exists(conn, "fund_bot_actions", "run_id"):
        conn.execute("ALTER TABLE fund_bot_actions ADD COLUMN run_id TEXT")
    if not _column_exists(conn, "fund_bot_orders", "order_run_id"):
        conn.execute("ALTER TABLE fund_bot_orders ADD COLUMN order_run_id TEXT")
    if not _column_exists(conn, "fund_bot_orders", "settle_run_id"):
        conn.execute("ALTER TABLE fund_bot_orders ADD COLUMN settle_run_id TEXT")

    # 2 张快照表：PK 含 run_id。若旧表 PK 不含 run_id，重建。
    for table, pk_cols, recreate_sql in (
        (
            "fund_bot_daily_snapshots",
            ("bot_id", "trade_date", "run_id"),
            """CREATE TABLE fund_bot_daily_snapshots (
                bot_id TEXT NOT NULL, trade_date TEXT NOT NULL,
                run_id TEXT NOT NULL DEFAULT '',
                initial_capital REAL, cash REAL, invested_value REAL, total_value REAL,
                net_value REAL, daily_return_pct REAL, cumulative_return_pct REAL,
                max_drawdown_pct REAL, equity_weight REAL, bond_weight REAL,
                gold_weight REAL, cash_weight REAL, holdings_json TEXT,
                PRIMARY KEY (bot_id, trade_date, run_id)
            )""",
        ),
        (
            "fund_bot_position_snapshots",
            ("bot_id", "fund_code", "trade_date", "run_id"),
            """CREATE TABLE fund_bot_position_snapshots (
                bot_id TEXT NOT NULL, fund_code TEXT NOT NULL, trade_date TEXT NOT NULL,
                run_id TEXT NOT NULL DEFAULT '',
                asset_class TEXT, role TEXT, shares REAL, nav REAL, market_value REAL,
                weight REAL, daily_pnl REAL, cumulative_return_pct REAL, holding_days INTEGER,
                PRIMARY KEY (bot_id, fund_code, trade_date, run_id)
            )""",
        ),
    ):
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        cur_pk_cols = tuple(r[1] for r in info if r[5] > 0)  # pk column index > 0
        # 已经包含 run_id 在 PK 中 → 跳过
        if "run_id" in cur_pk_cols:
            continue
        old_cols = [r[1] for r in info]
        copy_cols = [c for c in old_cols if c != "run_id"]  # run_id 列若已 ADD 也忽略，统一回填 ''
        copy_sql = ", ".join(copy_cols)
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}__old")
        conn.execute(recreate_sql)
        conn.execute(
            f"INSERT INTO {table} ({copy_sql}, run_id) "
            f"SELECT {copy_sql}, '' FROM {table}__old"
        )
        conn.execute(f"DROP TABLE {table}__old")

    # 列就位后再建 run_id 索引
    conn.executescript(_RUN_ID_INDEX_SQL)


def init_db():
    """创建数据库和所有表,并执行增量迁移。"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    _migrate_paradigm_columns(conn)
    _migrate_freeze_columns(conn)
    _migrate_run_id_columns(conn)
    conn.commit()
    conn.close()


@contextmanager
def get_conn():
    """获取数据库连接的上下文管理器"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
