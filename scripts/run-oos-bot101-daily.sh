#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORLD_DIR="$ROOT_DIR/world"
DB_PATH="${FUND_DB_PATH:-$ROOT_DIR/data/fund.db}"
CALENDAR_PATH="${CALENDAR_PATH:-$WORLD_DIR/runtime/calendar.json}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs}"
# Backward-compatible entrypoint name, now running the three OOS multi-fund bots by default.
OOS_BOTS_RAW="${OOS_BOTS:-bot101 bot102 bot103}"
LOCK="${LOCK:-/tmp/oos-bots-daily.lock}"
SQLITE3_BIN="${SQLITE3:-sqlite3}"
OOS_TABLES_SQL="${OOS_TABLES_SQL:-$ROOT_DIR/scripts/sql/oos_bot101_daily.sql}"
REQUIRE_PREV_TRADING="${REQUIRE_PREV_TRADING:-1}"
REQUIRE_TARGET_TRADING="${REQUIRE_TARGET_TRADING:-1}"
# 14:30 intraday mode. Keep the old env var name for existing cron/timer compatibility.
OOS_INTRADAY="${OOS_INTRADAY:-${OOS_BOT101_INTRADAY:-0}}"
MAX_DRIVER_SECONDS="${MAX_DRIVER_SECONDS:-1200}"
MAX_LOCK_AGE="${MAX_LOCK_AGE:-3600}"
OOS_RESTART_FUND_MCP="${OOS_RESTART_FUND_MCP:-1}"
FUND_MCP_BOT_ONLY_SERVICE="${FUND_MCP_BOT_ONLY_SERVICE:-lab-fund-bot-only.service}"
FUND_MCP_BOT_ONLY_HEALTH="${FUND_MCP_BOT_ONLY_HEALTH:-}"
CLI="$ROOT_DIR/fund-portfolio-mcp/cli_tools.py"
VENV_PY="$ROOT_DIR/fund-portfolio-mcp/.venv/bin/python"
PYTHON="${FUND_MCP_PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if [[ -x "$VENV_PY" ]]; then PYTHON="$VENV_PY"; else PYTHON="python3"; fi
fi
export OPENCLAW_ROOT="${OPENCLAW_ROOT:-$ROOT_DIR}"
export FUND_DB_PATH="$DB_PATH"

read -r -a OOS_BOT_LIST <<< "$(printf '%s' "$OOS_BOTS_RAW" | tr ',' ' ')"
if [[ "${#OOS_BOT_LIST[@]}" -eq 0 ]]; then
  echo "OOS_BOTS is empty" >&2
  exit 2
fi

run_id_for_bot() {
  local bot="$1" upper var val
  upper="${bot^^}"
  var="OOS_${upper}_RUN_ID"
  val="${!var:-}"
  if [[ -z "$val" && "$bot" == "bot101" ]]; then val="${OOS_BOT101_RUN_ID:-}"; fi
  if [[ -n "$val" ]]; then echo "$val"; else echo "oos-${bot}-daily"; fi
}

kill_tree() {
  local pid="$1" child
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do kill_tree "$child"; done
  kill -9 "$pid" 2>/dev/null || true
}

today_date() { date '+%F'; }

latest_trade_date() {
  node -e 'const fs=require("fs");const c=JSON.parse(fs.readFileSync(process.argv[1],"utf8")).trading_days;const t=process.argv[2];let r="";for(let i=c.length-1;i>=0;i--){if(c[i]<=t){r=c[i];break}}if(r)console.log(r);else process.exit(1)' "$CALENDAR_PATH" "$1"
}

prev_trade_date() {
  node -e 'const fs=require("fs");const c=JSON.parse(fs.readFileSync(process.argv[1],"utf8")).trading_days;const t=process.argv[2];let r="";for(let i=c.length-1;i>=0;i--){if(c[i]<t){r=c[i];break}}if(r)console.log(r);else process.exit(1)' "$CALENDAR_PATH" "$1"
}

is_trading_day() {
  node -e 'const fs=require("fs"); const cal=new Set(JSON.parse(fs.readFileSync(process.argv[1],"utf8")).trading_days); process.exit(cal.has(process.argv[2]) ? 0 : 1)' "$CALENDAR_PATH" "$1"
}

is_weekday() {
  node -e 'const d=new Date(`${process.argv[1]}T00:00:00+08:00`); const day=d.getDay(); process.exit(day >= 1 && day <= 5 ? 0 : 1)' "$1"
}

make_intraday_calendar() {
  local target="$1" tmp_dir="$2" tmp_calendar
  tmp_calendar="$tmp_dir/calendar.json"
  node -e '
const fs = require("fs");
const src = process.argv[1], dst = process.argv[2], target = process.argv[3];
const doc = JSON.parse(fs.readFileSync(src, "utf8"));
const days = Array.isArray(doc.trading_days) ? [...doc.trading_days] : [];
if (!days.includes(target)) { days.push(target); days.sort(); }
doc.trading_days = days;
fs.writeFileSync(dst, JSON.stringify(doc, null, 2) + String.fromCharCode(10));
' "$CALENDAR_PATH" "$tmp_calendar" "$target"
  echo "$tmp_calendar"
}

make_intraday_config() {
  local tmp_dir="$1" tmp_calendar="$2" src_config="$3" src_dir src_base tmp_config
  src_dir="$(dirname "$src_config")"
  src_base="$(basename "${src_config%.yaml}")"
  tmp_config="$src_dir/.${src_base}.intraday.$$.$RANDOM.yaml"
  node -e '
const fs = require("fs");
const src = process.argv[1], dst = process.argv[2], cal = process.argv[3];
const lines = fs.readFileSync(src, "utf8").split(/\r?\n/);
let done = false;
for (let i = 0; i < lines.length; i++) {
  if (/^calendar:\s*/.test(lines[i])) { lines[i] = `calendar: ${JSON.stringify(cal)}`; done = true; break; }
}
if (!done) lines.unshift(`calendar: ${JSON.stringify(cal)}`);
fs.writeFileSync(dst, lines.join(String.fromCharCode(10)));
' "$src_config" "$tmp_config" "$tmp_calendar"
  echo "$tmp_config"
}

TRADE_DATE="${1:-${TRADE_DATE:-}}"
if [[ -z "$TRADE_DATE" ]]; then
  if [[ "$OOS_INTRADAY" == "1" ]]; then
    TRADE_DATE="$(today_date)"
  else
    TRADE_DATE="$(latest_trade_date "$(today_date)")" || { echo "no trading day <= $(today_date) in calendar $CALENDAR_PATH" >&2; exit 2; }
  fi
fi
if [[ ! "$TRADE_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "bad TRADE_DATE: $TRADE_DATE" >&2
  exit 2
fi
for bot in "${OOS_BOT_LIST[@]}"; do
  if [[ ! "$bot" =~ ^[A-Za-z0-9._:-]+$ ]]; then echo "bad bot id: $bot" >&2; exit 2; fi
  rid="$(run_id_for_bot "$bot")"
  if [[ ! "$rid" =~ ^[A-Za-z0-9._:-]+$ ]]; then echo "bad run id for $bot: $rid" >&2; exit 2; fi
done

if [[ "$REQUIRE_PREV_TRADING" == "1" ]] && ! prev_trade_date "$TRADE_DATE" >/dev/null; then
  echo "[$(date '+%F %T')] skip: no prior trading day before $TRADE_DATE in calendar"
  exit 0
fi

INTRADAY_TMP_DIR=""
INTRADAY_CONFIG_FILES=""
DRIVER_CONFIG="$WORLD_DIR/config/world-multi-fund-backtest.yaml"
PREPASS_CONFIG="config/world-market-reports.yaml"
if [[ "$REQUIRE_TARGET_TRADING" == "1" ]] && ! is_trading_day "$TRADE_DATE"; then
  if [[ "$OOS_INTRADAY" == "1" ]]; then
    if ! is_weekday "$TRADE_DATE"; then
      echo "[$(date '+%F %T')] skip: intraday target day $TRADE_DATE is weekend and absent from calendar"
      exit 0
    fi
    INTRADAY_TMP_DIR="$(mktemp -d /tmp/oos-bots-intraday-calendar.XXXXXX)"
    trap '[[ -n "${INTRADAY_TMP_DIR:-}" ]] && rm -rf "$INTRADAY_TMP_DIR"; [[ -n "${INTRADAY_CONFIG_FILES:-}" ]] && rm -f $INTRADAY_CONFIG_FILES' EXIT
    INTRADAY_CALENDAR_PATH="$(make_intraday_calendar "$TRADE_DATE" "$INTRADAY_TMP_DIR")"
    DRIVER_CONFIG="$(make_intraday_config "$INTRADAY_TMP_DIR" "$INTRADAY_CALENDAR_PATH" "$WORLD_DIR/config/world-multi-fund-backtest.yaml")"
    PREPASS_CONFIG="$(make_intraday_config "$INTRADAY_TMP_DIR" "$INTRADAY_CALENDAR_PATH" "$WORLD_DIR/config/world-market-reports.yaml")"
    INTRADAY_CONFIG_FILES="$DRIVER_CONFIG $PREPASS_CONFIG"
    echo "[$(date '+%F %T')] WARN: intraday target day $TRADE_DATE absent from close-data calendar; using temporary calendar $INTRADAY_CALENDAR_PATH"
  else
    echo "[$(date '+%F %T')] skip: target day $TRADE_DATE is not a trading day"
    exit 0
  fi
fi

mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_FILE:-$LOG_DIR/oos-bots-daily-${TRADE_DATE}.log}"

exec 9>"$LOCK"
if ! flock -n 9; then
  holder_pid=""; holder_start=0
  read -r holder_pid holder_start < "${LOCK}.holder" 2>/dev/null || { holder_pid=""; holder_start=0; }
  lock_age=$(( $(date +%s) - ${holder_start:-0} ))
  if [[ "$lock_age" -gt "$MAX_LOCK_AGE" && -n "$holder_pid" ]] && kill -0 "$holder_pid" 2>/dev/null; then
    echo "[$(date '+%F %T')] WARN: stale OOS lock holder PID $holder_pid age=${lock_age}s; killing tree" >&2
    kill_tree "$holder_pid"
    sleep 3
    if ! flock -n 9; then echo "[$(date '+%F %T')] lock still busy after takeover; skip" >&2; exit 0; fi
  else
    echo "[$(date '+%F %T')] previous OOS daily run is still active (age=${lock_age}s holder=${holder_pid:-?}); skip" >&2
    exit 0
  fi
fi
printf '%s %s\n' "$$" "$(date +%s)" > "${LOCK}.holder"

exec > >(tee -a "$LOG_FILE") 2>&1
source "$ROOT_DIR/scripts/oos-bot101-mirror.lib.sh"

mirror_oos_results() {
  local bot="$1" run_id="$2"
  ensure_oos_tables
  mirror_oos_results_for_date "$TRADE_DATE" "$bot" "$run_id"
}

run_has_activity() {
  local bot="$1" run_id="$2"
  local n
  n="$($SQLITE3_BIN "$DB_PATH" "
    SELECT CASE WHEN
      EXISTS(SELECT 1 FROM fund_bot_daily_snapshots WHERE bot_id='$bot' AND run_id='$run_id') OR
      EXISTS(SELECT 1 FROM fund_bot_orders WHERE bot_id='$bot' AND order_run_id='$run_id') OR
      EXISTS(SELECT 1 FROM fund_bot_actions WHERE bot_id='$bot' AND run_id='$run_id') OR
      EXISTS(SELECT 1 FROM fund_bot_holdings WHERE bot_id='$bot' AND run_id='$run_id')
    THEN 1 ELSE 0 END;")"
  [[ "${n:-0}" != "0" ]]
}

ensure_first_run_account() {
  local bot="$1" run_id="$2"
  if run_has_activity "$bot" "$run_id"; then return 0; fi
  echo "[$(date '+%F %T')] first OOS run for $bot/$run_id: reset account to initial capital once"
  "$PYTHON" "$CLI" init_fund_account --bot-id "$bot" --run-id "$run_id" --initial-capital 1000000 --reset >/dev/null
}

restart_fund_mcp_bot_only() {
  if [[ "$OOS_RESTART_FUND_MCP" != "1" ]]; then return 0; fi
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "[$(date '+%F %T')] WARN: systemctl not found; skip fund MCP restart" >&2
    return 0
  fi
  if ! systemctl --user status "$FUND_MCP_BOT_ONLY_SERVICE" >/dev/null 2>&1; then
    echo "[$(date '+%F %T')] WARN: $FUND_MCP_BOT_ONLY_SERVICE not found/inactive; skip managed restart" >&2
    return 0
  fi
  echo "[$(date '+%F %T')] restarting $FUND_MCP_BOT_ONLY_SERVICE before bot decisions"
  systemctl --user restart "$FUND_MCP_BOT_ONLY_SERVICE"
  sleep 2
  if ! systemctl --user is-active --quiet "$FUND_MCP_BOT_ONLY_SERVICE"; then
    echo "[$(date '+%F %T')] ERROR: $FUND_MCP_BOT_ONLY_SERVICE is not active after restart" >&2
    exit 2
  fi
  if [[ -n "$FUND_MCP_BOT_ONLY_HEALTH" ]] && command -v curl >/dev/null 2>&1; then
    curl -fsS "$FUND_MCP_BOT_ONLY_HEALTH" >/dev/null || echo "[$(date '+%F %T')] WARN: fund MCP health probe failed: $FUND_MCP_BOT_ONLY_HEALTH" >&2
  fi
}

bot_stage_artifacts_ok() {
  local bot="$1" run_id="$2" day_dir status_file
  day_dir="$WORLD_DIR/runtime/runs/$run_id/$TRADE_DATE/$bot"
  status_file="$day_dir/status.json"
  [[ -s "$day_dir/reply.json" && -s "$day_dir/close_my_day.json" && -s "$status_file" ]] || return 1
  grep -q '"status"[[:space:]]*:[[:space:]]*"ok"' "$status_file"
}

validate_new_order_dates() {
  local bot="$1" run_id="$2" before_id="$3" n
  n="$($SQLITE3_BIN "$DB_PATH" "
    SELECT COUNT(*) FROM fund_bot_orders
     WHERE bot_id='$bot'
       AND order_run_id='$run_id'
       AND order_id > $before_id
       AND order_date <> '$TRADE_DATE';")"
  if [[ "${n:-0}" == "0" ]]; then return 0; fi
  echo "[$(date '+%F %T')] ERROR: $bot/$run_id wrote new orders outside TRADE_DATE=$TRADE_DATE" >&2
  "$SQLITE3_BIN" -header -column "$DB_PATH" "
    SELECT order_id, order_date, fund_code, order_type, ROUND(order_amount,2) AS amount, status, pricing_status
      FROM fund_bot_orders
     WHERE bot_id='$bot'
       AND order_run_id='$run_id'
       AND order_id > $before_id
       AND order_date <> '$TRADE_DATE'
     ORDER BY order_id;" >&2 || true
  return 1
}

summary() {
  local status="$1"
  echo
  echo "===== OOS bots daily health summary ====="
  echo "finished_at: $(date '+%F %T %z')"
  echo "status: $status"
  echo "prepass_market_reports_rc: ${prepass_rc:-NA}"
  echo "report_date: $TRADE_DATE"
  echo "bots: ${OOS_BOT_LIST[*]}"
  echo "tables: oos_market_report_status, oos_bot_daily_snapshots, oos_bot_position_snapshots, oos_bot_orders, oos_bot_actions"
  for bot in "${OOS_BOT_LIST[@]}"; do
    local run_id
    run_id="$(run_id_for_bot "$bot")"
    echo
    echo "-- $bot run_id=$run_id orders"
    "$SQLITE3_BIN" -header -column "$DB_PATH" "SELECT order_date, order_type, fund_code, ROUND(order_amount,2) AS amount, ROUND(reference_nav,4) AS nav, status, substr(coalesce(action_reason,''),1,80) AS reason FROM oos_bot_orders WHERE bot_id='$bot' AND live_run_id='$run_id' AND order_date='$TRADE_DATE' ORDER BY source_order_id;" || true
    echo "-- $bot positions"
    "$SQLITE3_BIN" -header -column "$DB_PATH" "SELECT trade_date, fund_code, ROUND(weight * 100, 2) AS weight_pct, ROUND(market_value, 2) AS market_value, ROUND(shares, 4) AS shares FROM oos_bot_position_snapshots WHERE bot_id='$bot' AND live_run_id='$run_id' AND trade_date='$TRADE_DATE' ORDER BY weight DESC, fund_code;" || true
    echo "-- $bot nav"
    "$SQLITE3_BIN" -header -column "$DB_PATH" "SELECT trade_date, ROUND(total_value,2) AS total_value, ROUND(net_value,6) AS net_value, ROUND(cumulative_return_pct,4) AS cumulative_return_pct, ROUND(cash_weight * 100, 2) AS cash_weight_pct FROM oos_bot_daily_snapshots WHERE bot_id='$bot' AND live_run_id='$run_id' AND trade_date='$TRADE_DATE';" || true
  done
  echo "===== end summary ====="
}

status=failed
trap '[[ -n "${INTRADAY_TMP_DIR:-}" ]] && rm -rf "$INTRADAY_TMP_DIR"; [[ -n "${INTRADAY_CONFIG_FILES:-}" ]] && rm -f $INTRADAY_CONFIG_FILES; summary "$status"' EXIT

ensure_oos_tables

echo "[$(date '+%F %T')] OOS bots daily start date=$TRADE_DATE bots=${OOS_BOT_LIST[*]} log=$LOG_FILE"
restart_fund_mcp_bot_only
prepass_rc=0
RUN_ID="market-reports-daily-${TRADE_DATE}" CALENDAR_PATH="${INTRADAY_CALENDAR_PATH:-$CALENDAR_PATH}" CONFIG_PATH="$PREPASS_CONFIG" "$ROOT_DIR/scripts/run-oos-market-reports-daily.sh" "$TRADE_DATE" || prepass_rc=$?
if [[ "$prepass_rc" != "0" ]]; then
  echo "[$(date '+%F %T')] WARN: prepass(market-reports) rc=$prepass_rc; continue bot decisions" >&2
fi

overall_rc=0
declare -A BOT_RUN_IDS=()
declare -A BOT_BEFORE_ORDER_IDS=()
declare -A BOT_STAGE_RCS=()
declare -A BOT_PIDS=()
declare -a LAUNCHED_BOTS=()

for bot in "${OOS_BOT_LIST[@]}"; do
  run_id="$(run_id_for_bot "$bot")"
  BOT_RUN_IDS["$bot"]="$run_id"
  before_order_id="$($SQLITE3_BIN "$DB_PATH" "SELECT COALESCE(MAX(order_id), 0) FROM fund_bot_orders WHERE bot_id='$bot' AND order_run_id='$run_id';")"
  BOT_BEFORE_ORDER_IDS["$bot"]="$before_order_id"

  if [[ "${FORCE_BOT:-0}" != "1" ]] && [[ -d "$WORLD_DIR/runtime/runs/$run_id/$TRADE_DATE/$bot" ]]; then
    echo "[$(date '+%F %T')] $bot already ran real decision for $TRADE_DATE (run dir exists); skip bot stage (set FORCE_BOT=1 to rerun)"
    BOT_STAGE_RCS["$bot"]=0
    continue
  fi

  ensure_first_run_account "$bot" "$run_id"
  echo "[$(date '+%F %T')] launching OOS $bot daily in parallel date=$TRADE_DATE live_run_id=$run_id"
  (
    driver_rc=0
    (
      cd "$WORLD_DIR"
      OOS_BOTS="$bot" TRADE_DATE="$TRADE_DATE" timeout --kill-after=60s "${MAX_DRIVER_SECONDS}s" \
        node --experimental-strip-types src/oos-daily-driver.ts \
        --date "$TRADE_DATE" \
        --bot-id "$bot" \
        --run-id "$run_id" \
        --config "$DRIVER_CONFIG" \
        --world-dir runtime
    ) || driver_rc=$?
    if [[ "$driver_rc" == "124" || "$driver_rc" == "137" ]]; then
      if bot_stage_artifacts_ok "$bot" "$run_id"; then
        echo "[$(date '+%F %T')] WARN: oos-daily-driver $bot timed out after artifacts were written(rc=$driver_rc); treating bot stage as ok" >&2
        driver_rc=0
      else
        echo "[$(date '+%F %T')] WARN: oos-daily-driver $bot timed out after ${MAX_DRIVER_SECONDS}s(rc=$driver_rc); continue mirror" >&2
      fi
    elif [[ "$driver_rc" != "0" ]]; then
      echo "[$(date '+%F %T')] WARN: oos-daily-driver $bot failed rc=$driver_rc; continue mirror" >&2
    fi
    exit "$driver_rc"
  ) &
  BOT_PIDS["$bot"]=$!
  LAUNCHED_BOTS+=("$bot")
done

for bot in "${LAUNCHED_BOTS[@]}"; do
  pid="${BOT_PIDS[$bot]}"
  if wait "$pid"; then
    BOT_STAGE_RCS["$bot"]=0
  else
    BOT_STAGE_RCS["$bot"]=$?
  fi
  echo "[$(date '+%F %T')] OOS $bot parallel stage finished rc=${BOT_STAGE_RCS[$bot]}"
done

for bot in "${OOS_BOT_LIST[@]}"; do
  run_id="${BOT_RUN_IDS[$bot]}"
  before_order_id="${BOT_BEFORE_ORDER_IDS[$bot]:-0}"
  driver_rc="${BOT_STAGE_RCS[$bot]:-0}"
  if ! validate_new_order_dates "$bot" "$run_id" "$before_order_id"; then driver_rc=98; fi
  mirror_oos_results "$bot" "$run_id"
  if [[ "$driver_rc" != "0" ]]; then overall_rc="$driver_rc"; fi
  echo "[$(date '+%F %T')] OOS $bot daily done date=$TRADE_DATE live_run_id=$run_id driver_rc=$driver_rc"
done
if [[ "$overall_rc" == "0" ]]; then status=ok; else status="driver_rc_${overall_rc}"; fi
