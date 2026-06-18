-- OOS bot101 daily cron result tables.
--
-- fund_bot_* remains the execution ledger because fund-portfolio-mcp tools read
-- and write those tables. These oos_* tables are the isolated reporting/audit
-- surface for cron, so backtest dashboards and research queries can distinguish
-- daily OOS account results from replay/backtest runs.

CREATE TABLE IF NOT EXISTS oos_market_report_status (
  live_run_id         TEXT NOT NULL,
  as_of_date          TEXT NOT NULL,
  report_type         TEXT NOT NULL,
  source_report_id    INTEGER,
  source_agent_run_id TEXT,
  generated_at        TEXT,
  chars               INTEGER,
  captured_at         TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (live_run_id, as_of_date, report_type)
);
CREATE INDEX IF NOT EXISTS idx_oos_market_report_status_date
  ON oos_market_report_status(as_of_date, report_type);

CREATE TABLE IF NOT EXISTS oos_bot_daily_snapshots (
  live_run_id            TEXT NOT NULL,
  bot_id                 TEXT NOT NULL,
  trade_date             TEXT NOT NULL,
  initial_capital        REAL,
  cash                   REAL,
  invested_value         REAL,
  total_value            REAL,
  net_value              REAL,
  daily_return_pct       REAL,
  cumulative_return_pct  REAL,
  max_drawdown_pct       REAL,
  equity_weight          REAL,
  bond_weight            REAL,
  gold_weight            REAL,
  cash_weight            REAL,
  cash_receivable        REAL,
  holdings_json          TEXT,
  source_run_id          TEXT NOT NULL,
  captured_at            TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (live_run_id, bot_id, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_oos_bot_daily_snapshots_bot_date
  ON oos_bot_daily_snapshots(bot_id, trade_date);

CREATE TABLE IF NOT EXISTS oos_bot_position_snapshots (
  live_run_id              TEXT NOT NULL,
  bot_id                   TEXT NOT NULL,
  fund_code                TEXT NOT NULL,
  trade_date               TEXT NOT NULL,
  asset_class              TEXT,
  role                     TEXT,
  shares                   REAL,
  nav                      REAL,
  market_value             REAL,
  weight                   REAL,
  daily_pnl                REAL,
  cumulative_return_pct    REAL,
  holding_days             INTEGER,
  source_run_id            TEXT NOT NULL,
  captured_at              TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (live_run_id, bot_id, trade_date, fund_code)
);
CREATE INDEX IF NOT EXISTS idx_oos_bot_position_snapshots_bot_date
  ON oos_bot_position_snapshots(bot_id, trade_date);

CREATE TABLE IF NOT EXISTS oos_bot_actions (
  live_run_id       TEXT NOT NULL,
  bot_id            TEXT NOT NULL,
  action_date       TEXT NOT NULL,
  source_action_id  INTEGER NOT NULL,
  review_id         INTEGER,
  fund_code         TEXT,
  action_type       TEXT,
  before_weight     REAL,
  after_weight      REAL,
  nav_used          REAL,
  amount            REAL,
  shares            REAL,
  fee               REAL,
  reason            TEXT,
  paradigm          TEXT,
  source_run_id     TEXT NOT NULL,
  captured_at       TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (live_run_id, source_action_id)
);
CREATE INDEX IF NOT EXISTS idx_oos_bot_actions_bot_date
  ON oos_bot_actions(bot_id, action_date, live_run_id);

CREATE TABLE IF NOT EXISTS oos_bot_orders (
  live_run_id       TEXT NOT NULL,
  bot_id            TEXT NOT NULL,
  order_date        TEXT NOT NULL,
  source_order_id   INTEGER NOT NULL,
  fund_code         TEXT,
  order_type        TEXT,
  order_amount      REAL,
  reference_nav     REAL,
  status            TEXT,
  confirm_date      TEXT,
  confirmed_amount  REAL,
  confirmed_shares  REAL,
  action_reason     TEXT,
  source_run_id     TEXT NOT NULL,
  settle_run_id     TEXT,
  captured_at       TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (live_run_id, source_order_id)
);
CREATE INDEX IF NOT EXISTS idx_oos_bot_orders_bot_date
  ON oos_bot_orders(bot_id, order_date, live_run_id);
