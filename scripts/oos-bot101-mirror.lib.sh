# Shared mirror helpers for fixed OOS bot runs.
# Sourced by run-oos-bot101-daily.sh and refresh-oos-bot101-nav.sh.
# Caller must define: DB_PATH, SQLITE3_BIN, OOS_TABLES_SQL, RUN_ID.
# Optional: BOT_ID (default bot101).

ensure_oos_tables() {
  "$SQLITE3_BIN" "$DB_PATH" < "$OOS_TABLES_SQL"
}

# mirror_oos_results_for_date <trade_date> [bot_id] [run_id]
# Re-mirror one trading day from the live fund_bot_* tables into the oos_* tables.
# DELETE + INSERT per table -> idempotent, safe to re-run after snapshots are recomputed.
# NAV/position snapshots are mirrored only through MAX(fund_nav.nav_date); orders/actions/reports
# may still be mirrored for the decision date before same-day fund NAV is published.
mirror_oos_results_for_date() {
  local TRADE_DATE="$1"
  local MIRROR_BOT_ID="${2:-${BOT_ID:-bot101}}"
  local MIRROR_RUN_ID="${3:-$RUN_ID}"
  "$SQLITE3_BIN" "$DB_PATH" <<SQL
BEGIN;
DELETE FROM oos_market_report_status
 WHERE live_run_id='$MIRROR_RUN_ID' AND as_of_date='$TRADE_DATE';
INSERT INTO oos_market_report_status
  (live_run_id, as_of_date, report_type, source_report_id, source_agent_run_id, generated_at, chars, captured_at)
SELECT '$MIRROR_RUN_ID', as_of_date, report_type, id, agent_run_id, generated_at, length(content_md), datetime('now')
  FROM market_reports
 WHERE scope='global'
   AND as_of_date='$TRADE_DATE'
   AND report_type IN ('market_context','market_mainline','mainline_rotation','market_mainline_daily','mainline_rotation_daily');

DELETE FROM oos_bot_daily_snapshots
 WHERE live_run_id='$MIRROR_RUN_ID' AND bot_id='$MIRROR_BOT_ID' AND trade_date='$TRADE_DATE';
INSERT INTO oos_bot_daily_snapshots
  (live_run_id, bot_id, trade_date, initial_capital, cash, invested_value, total_value,
   net_value, daily_return_pct, cumulative_return_pct, max_drawdown_pct,
   equity_weight, bond_weight, gold_weight, cash_weight, cash_receivable,
   holdings_json, source_run_id, captured_at)
SELECT '$MIRROR_RUN_ID', bot_id, trade_date, initial_capital, cash, invested_value, total_value,
       net_value, daily_return_pct, cumulative_return_pct, max_drawdown_pct,
       equity_weight, bond_weight, gold_weight, cash_weight, cash_receivable,
       holdings_json, run_id, datetime('now')
  FROM fund_bot_daily_snapshots
 WHERE bot_id='$MIRROR_BOT_ID' AND run_id='$MIRROR_RUN_ID' AND trade_date='$TRADE_DATE'
   AND trade_date <= (SELECT MAX(nav_date) FROM fund_nav);

DELETE FROM oos_bot_position_snapshots
 WHERE live_run_id='$MIRROR_RUN_ID' AND bot_id='$MIRROR_BOT_ID' AND trade_date='$TRADE_DATE';
INSERT INTO oos_bot_position_snapshots
  (live_run_id, bot_id, fund_code, trade_date, asset_class, role, shares, nav,
   market_value, weight, daily_pnl, cumulative_return_pct, holding_days,
   source_run_id, captured_at)
SELECT '$MIRROR_RUN_ID', bot_id, fund_code, trade_date, asset_class, role, shares, nav,
       market_value, weight, daily_pnl, cumulative_return_pct, holding_days,
       run_id, datetime('now')
  FROM fund_bot_position_snapshots
 WHERE bot_id='$MIRROR_BOT_ID' AND run_id='$MIRROR_RUN_ID' AND trade_date='$TRADE_DATE'
   AND trade_date <= (SELECT MAX(nav_date) FROM fund_nav);

DELETE FROM oos_bot_orders
 WHERE live_run_id='$MIRROR_RUN_ID' AND bot_id='$MIRROR_BOT_ID' AND order_date='$TRADE_DATE';
INSERT INTO oos_bot_orders
  (live_run_id, bot_id, order_date, source_order_id, fund_code, order_type,
   order_amount, reference_nav, status, confirm_date, confirmed_amount,
   confirmed_shares, action_reason, source_run_id, settle_run_id, captured_at)
SELECT '$MIRROR_RUN_ID', bot_id, order_date, order_id, fund_code, order_type,
       order_amount, reference_nav, status, confirm_date, confirmed_amount,
       confirmed_shares, action_reason, order_run_id, settle_run_id, datetime('now')
  FROM fund_bot_orders
 WHERE bot_id='$MIRROR_BOT_ID' AND order_run_id='$MIRROR_RUN_ID' AND order_date='$TRADE_DATE';

DELETE FROM oos_bot_actions
 WHERE live_run_id='$MIRROR_RUN_ID' AND bot_id='$MIRROR_BOT_ID' AND action_date='$TRADE_DATE';
INSERT INTO oos_bot_actions
  (live_run_id, bot_id, action_date, source_action_id, review_id, fund_code,
   action_type, before_weight, after_weight, nav_used, amount, shares, fee,
   reason, paradigm, source_run_id, captured_at)
SELECT '$MIRROR_RUN_ID', bot_id, action_date, action_id, review_id, fund_code,
       action_type, before_weight, after_weight, nav_used, amount, shares, fee,
       reason, paradigm, run_id, datetime('now')
  FROM fund_bot_actions
 WHERE bot_id='$MIRROR_BOT_ID' AND run_id='$MIRROR_RUN_ID' AND action_date='$TRADE_DATE';
COMMIT;
SQL
}
