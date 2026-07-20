#!/usr/bin/env bash
set -euo pipefail

# OOS multi-bot NAV sync after fund_nav refresh.
# Keeps the old filename for timer compatibility; default bots are bot101/102/103.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORLD_DIR="$ROOT_DIR/world"
DB_PATH="${FUND_DB_PATH:-$ROOT_DIR/data/fund.db}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs}"
OOS_BOTS_RAW="${OOS_BOTS:-bot101 bot102 bot103}"
LOCK="${LOCK:-/tmp/refresh-oos-bots-nav.lock}"
SQLITE3_BIN="${SQLITE3:-sqlite3}"
OOS_TABLES_SQL="${OOS_TABLES_SQL:-$ROOT_DIR/scripts/sql/oos_bot101_daily.sql}"
LOOKBACK_TRADING_DAYS="${LOOKBACK_TRADING_DAYS:-10}"
CLI="$ROOT_DIR/fund-portfolio-mcp/cli_tools.py"

VENV_PY="$ROOT_DIR/fund-portfolio-mcp/.venv/bin/python"
PYTHON="${FUND_MCP_PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if [[ -x "$VENV_PY" ]]; then PYTHON="$VENV_PY"; else PYTHON="python3"; fi
fi
export OPENCLAW_ROOT="${OPENCLAW_ROOT:-$ROOT_DIR}"
export FUND_DB_PATH="$DB_PATH"

read -r -a OOS_BOT_LIST <<< "$(printf '%s' "$OOS_BOTS_RAW" | tr ',' ' ')"
if [[ "${#OOS_BOT_LIST[@]}" -eq 0 ]]; then echo "OOS_BOTS is empty" >&2; exit 2; fi

run_id_for_bot() {
  local bot="$1" upper var val
  upper="${bot^^}"
  var="OOS_${upper}_RUN_ID"
  val="${!var:-}"
  if [[ -z "$val" && "$bot" == "bot101" ]]; then val="${OOS_BOT101_RUN_ID:-}"; fi
  if [[ -n "$val" ]]; then echo "$val"; else echo "oos-${bot}-daily"; fi
}

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date '+%F %T')] previous OOS nav refresh still active; skip" >&2
  exit 0
fi

mkdir -p "$LOG_DIR"
source "$ROOT_DIR/scripts/oos-bot101-mirror.lib.sh"
ensure_oos_tables

NAV_MAX="$($SQLITE3_BIN "$DB_PATH" "SELECT MAX(nav_date) FROM fund_nav;")"
if [[ ! "$NAV_MAX" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "[$(date '+%F %T')] fund_nav empty or bad MAX(nav_date)='$NAV_MAX'; abort" >&2
  exit 1
fi

refresh_one_bot() {
  local bot="$1" run_id="$2" first_date settle_as_of
  first_date="$($SQLITE3_BIN "$DB_PATH" "
    SELECT MIN(d) FROM (
      SELECT MIN(trade_date) AS d FROM fund_bot_daily_snapshots WHERE bot_id='$bot' AND run_id='$run_id'
      UNION ALL SELECT MIN(order_date) FROM fund_bot_orders WHERE bot_id='$bot' AND order_run_id='$run_id'
      UNION ALL SELECT MIN(action_date) FROM fund_bot_actions WHERE bot_id='$bot' AND run_id='$run_id'
    ) WHERE d IS NOT NULL;")"
  if [[ -z "$first_date" ]]; then
    echo "[$(date '+%F %T')] $bot/$run_id has no activity yet; skip"
    return 0
  fi

  mapfile -t DATES < <("$SQLITE3_BIN" "$DB_PATH" "
    SELECT nav_date FROM (
      SELECT DISTINCT nav_date FROM fund_nav
      WHERE nav_date <= '$NAV_MAX' AND nav_date >= '$first_date'
      ORDER BY nav_date DESC LIMIT $LOOKBACK_TRADING_DAYS
    ) ORDER BY nav_date ASC;")
  if [[ "${#DATES[@]}" -eq 0 ]]; then
    echo "[$(date '+%F %T')] $bot/$run_id no trading days in window (nav_max=$NAV_MAX first=$first_date); skip"
    return 0
  fi

  echo "[$(date '+%F %T')] nav refresh $bot/$run_id nav_max=$NAV_MAX window=${DATES[0]}..${DATES[-1]} (${#DATES[@]} trading days)"
  settle_as_of="${SETTLE_AS_OF:-$(date +%F)}"
  "$PYTHON" "$CLI" settle_pending_orders --bot-id "$bot" --run-id "$run_id" --as-of-date "$settle_as_of" >/dev/null
  echo "[$(date '+%F %T')]   $bot pending settled as_of=$settle_as_of"
  for D in "${DATES[@]}"; do
    "$PYTHON" "$CLI" close_my_day --bot-id "$bot" --run-id "$run_id" --trade-date "$D" >/dev/null
    echo "[$(date '+%F %T')]   $bot $D snapshot recomputed"
  done
  for D in "${DATES[@]}"; do
    mirror_oos_results_for_date "$D" "$bot" "$run_id"
  done
  echo "[$(date '+%F %T')]   $bot mirrored ${#DATES[@]} days into oos_* tables"

  "$SQLITE3_BIN" -header -column "$DB_PATH" "
    SELECT trade_date, ROUND(total_value,2) AS total_value, ROUND(net_value,6) AS net_value,
           ROUND(cumulative_return_pct,4) AS cum_ret_pct, ROUND(cash_weight*100,2) AS cash_pct
    FROM oos_bot_daily_snapshots
    WHERE live_run_id='$run_id' AND bot_id='$bot'
    ORDER BY trade_date DESC LIMIT 3;"
}

echo "[$(date '+%F %T')] OOS nav refresh start bots=${OOS_BOT_LIST[*]} nav_max=$NAV_MAX"
for bot in "${OOS_BOT_LIST[@]}"; do
  refresh_one_bot "$bot" "$(run_id_for_bot "$bot")"
done
echo "[$(date '+%F %T')] OOS nav refresh done"
